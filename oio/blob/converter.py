# Copyright (C) 2018 OpenIO SAS, as part of OpenIO SDS
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.


import time
import os
import tempfile
from string import hexdigits
from datetime import datetime
from collections import OrderedDict

from oio.common.constants import chunk_xattr_keys, OIO_VERSION, \
    STRLEN_CHUNKID
from oio.common.utils import cid_from_name, paths_gen
from oio.common.fullpath import decode_fullpath, decode_old_fullpath, \
    encode_fullpath
from oio.common.xattr import modify_xattr
from oio.common.exceptions import ContentNotFound, OrphanChunk, \
    ConfigurationException
from oio.common.logger import get_logger
from oio.common.green import ratelimit
from oio.common.easy_value import int_value, is_hexa, true_value
from oio.container.client import ContainerClient
from oio.content.factory import ContentFactory
from oio.blob.utils import read_chunk_metadata, check_volume


XATTR_CHUNK_ID = chunk_xattr_keys['chunk_id']
XATTR_OLD_FULLPATH = 'oio:'
XATTR_OLD_FULLPATH_SIZE = 4


class CacheDict(OrderedDict):

    def __init__(self, size=262144):
        super(CacheDict, self).__init__()
        self.size = size
        self._check_size()

    def __setitem__(self, key, value):
        super(CacheDict, self).__setitem__(key, value)
        self._check_size()

    def _check_size(self):
        while len(self) > self.size:
            self.popitem(last=False)


class BlobConverter(object):

    def __init__(self, conf, logger=None, **kwargs):
        self.conf = conf
        self.logger = logger or get_logger(conf)
        volume = conf.get('volume')
        if not volume:
            raise ConfigurationException(
                'No volume specified for converter')
        self.volume = volume
        self.namespace, self.volume_id = check_volume(self.volume)
        # cache
        self.name_by_cid = CacheDict()
        self.content_id_by_name = CacheDict()
        # client
        self.container_client = ContainerClient(conf)
        self.content_factory = ContentFactory(conf, logger=self.logger)
        # stats/logs
        self.errors = 0
        self.passes = 0
        self.total_chunks_processed = 0
        self.start_time = 0
        self.last_reported = 0
        self.report_interval = int_value(
            conf.get('report_interval'), 3600)
        # speed
        self.chunks_run_time = 0
        self.max_chunks_per_second = int_value(
            conf.get('chunks_per_second'), 30)
        # backup
        self.backup = true_value(conf.get('backup', True))
        self.backup_dir = conf.get('backup_dir', tempfile.gettempdir())
        self.backup_name = 'backup_%s_%f' \
            % (self.volume_id, time.time())

    def save_xattr(self, chunk_id, xattr):
        if not self.backup:
            return
        dirname = self.backup_dir + '/' + self.backup_name + '/' + chunk_id[:3]
        try:
            os.makedirs(dirname)
        except OSError:
            if not os.path.isdir(dirname):
                raise
        with open(dirname + '/' + chunk_id, 'w') as backup_fd:
            # same format as getfattr
            backup_fd.write('# file: ' + self._get_path(chunk_id) + '\n')
            for k, v in xattr.iteritems():
                backup_fd.write('user.' + k + '="' + v + '"\n')

    def _save_container(self, cid, account, container):
        cid = cid.upper()
        self.name_by_cid[cid] = (account, container)
        return cid, account, container

    def _save_content(self, cid, path, version, content_id):
        cid = cid.upper()
        content_id = content_id.upper()
        self.content_id_by_name[(cid, path, version)] = content_id
        return cid, path, version, content_id

    def cid_from_name(self, account, container):
        cid = cid_from_name(account, container)
        cid, account, container = self._save_container(cid, account, container)
        return cid

    def name_from_cid(self, cid):
        name = self.name_by_cid.get(cid)
        if name:
            return name

        properties = self.container_client.container_get_properties(
            cid=cid)
        account = properties['system']['sys.account']
        container = properties['system']['sys.user.name']
        cid, account, container = self._save_container(cid, account, container)
        return account, container

    def content_id_from_name(self, cid, path, version, search=False):
        content_id = self.content_id_by_name.get((cid, path, version))
        if content_id or not search:
            return content_id

        properties = self.container_client.content_show(
            cid=cid, path=path, version=version)
        content_id = properties['id']
        cid, path, version, content_id = self._save_content(
            cid, path, version, content_id)
        return content_id

    def decode_fullpath(self, fullpath):
        account, container, path, version, content_id = decode_fullpath(
            fullpath)
        cid = self.cid_from_name(account, container)
        cid, path, version, content_id = self._save_content(
            cid, path, version, content_id)
        return account, container, cid, path, version, content_id

    def decode_old_fullpath(self, old_fullpath):
        account, container, path, version = decode_old_fullpath(old_fullpath)
        cid = self.cid_from_name(account, container)
        content_id = self.content_id_from_name(cid, path, version)
        return account, container, cid, path, version, content_id

    def encode_fullpath(self, chunk_inode, chunk_id,
                        account, container, path, version, content_id):
        # check if chunk exists and has the same inode
        if not is_hexa(chunk_id) or len(chunk_id) != STRLEN_CHUNKID:
            raise ValueError('chunk ID must be hexadecimal (%s)'
                             % STRLEN_CHUNKID)
        try:
            chunk_inode2 = os.stat(
                self.volume + '/' + chunk_id[:3] + '/' + chunk_id).st_ino
        except OSError:
            raise OrphanChunk('No such chunk: possible orphan chunk')
        if chunk_inode2 != chunk_inode:
            raise OrphanChunk('Not the same inode: possible orphan chunk')

        # check fullpath and chunk ID
        if isinstance(version, basestring):
            try:
                version = int(version)
            except ValueError:
                raise ValueError('version must be a number')
        if version <= 0:
            raise ValueError('version must be positive')
        if not is_hexa(content_id):
            raise ValueError('content ID must be hexadecimal')

        fullpath = encode_fullpath(
            account, container, path, version, content_id.upper())

        return chunk_id.upper(), fullpath

    def _get_chunk_id_and_fullpath(self, chunk_inode, chunk_pos, content,
                                   chunk_id=None):
        content.container_id, content.account, content.container_name = \
            self._save_container(content.container_id, content.account,
                                 content.container_name)
        content.container_id, content.path, content.version, \
            content.content_id = self._save_content(
                content.container_id, content.path, content.version,
                content.content_id)

        chunks = content.chunks.filter(host=self.volume_id)
        if chunk_id:
            chunks = chunks.filter(id=chunk_id)
        chunk = chunks.filter(pos=chunk_pos).one()
        if chunk is None:
            raise OrphanChunk('Chunk not found in content:'
                              'possible orphan chunk')

        chunk_id, new_fullpath = self.encode_fullpath(
            chunk_inode, chunk.id, content.account, content.container_name,
            content.path, content.version, content.content_id)
        return chunk_id, new_fullpath

    def get_chunk_id_and_fullpath(
            self, chunk_inode, chunk_pos, container_id, path, version,
            chunk_id=None, account=None, container=None, content_id=None):
        if account is None or container is None:
            account, container = self.name_from_cid(container_id)

        if content_id:
            try:
                content = self.content_factory.get(
                    container_id, content_id,
                    account=account, container_name=container)
                return self._get_chunk_id_and_fullpath(
                    chunk_inode, chunk_pos, content, chunk_id=chunk_id)
            except Exception as exc:
                self.logger.warn(
                    'chunk_id=%s chunk_pos=%s object=%s/%s/%s/%s/%s/%s: %s',
                    chunk_id, chunk_pos, str(account), str(container),
                    container_id, path, str(version), str(content_id), exc)

        # version must be integer
        try:
            version = str(int(version))
        except Exception:
            version = None

        try:
            content = self.content_factory.get_by_path_and_version(
                container_id, path, version,
                account=account, container_name=container)
        except ContentNotFound:
            raise OrphanChunk('Content not found: possible orphan chunk')
        return self._get_chunk_id_and_fullpath(
                chunk_inode, chunk_pos, content, chunk_id=chunk_id)

    def convert_chunk(self, fd, chunk_id):
        meta, raw_meta = read_chunk_metadata(fd, chunk_id,
                                             check_chunk_id=False)

        links = meta.get('links', dict())
        for chunk_id2, fullpath2 in links.iteritems():
            self.decode_fullpath(fullpath2)

        fullpath = meta.get('full_path')
        if fullpath is not None:
            self.decode_fullpath(fullpath)
            if meta.get('oio_version') == OIO_VERSION:
                return True, meta

        chunk_inode = os.fstat(fd.fileno()).st_ino
        raw_chunk_id = None
        chunk_id = chunk_id.upper()
        chunk_pos = meta['chunk_pos']
        container_id = meta['container_id'].upper()
        path = meta['content_path']
        version = meta['content_version']
        content_id = meta['content_id'].upper()

        new_fullpaths = dict()
        xattr_to_remove = list()
        success = True

        for k, v in raw_meta.iteritems():
            # fetch raw chunk ID
            if k == XATTR_CHUNK_ID:
                raw_chunk_id = v.upper()

            # search old fullpaths
            if not k.startswith(XATTR_OLD_FULLPATH) \
                    or not is_hexa(k[4:], size=64):
                continue

            try:
                account2, container2, container_id2, path2, version2, \
                    content_id2 = self.decode_old_fullpath(v)

                if container_id == container_id2 and path == path2 \
                        and version == version2:
                    if content_id2 is None:
                        content_id2 = self.content_id_from_name(
                            container_id2, path2, version2, search=True)

                    chunk_id, new_fullpath = self.encode_fullpath(
                        chunk_inode, chunk_id, account2, container2, path2,
                        version2, content_id2)
                    new_fullpaths[chunk_id] = new_fullpath
                else:
                    chunk_id2, new_fullpath = self.get_chunk_id_and_fullpath(
                        chunk_inode, chunk_pos, container_id2, path2, version2,
                        account=account2, container=container2,
                        content_id=content_id2)
                    new_fullpaths[chunk_id2] = new_fullpath

                xattr_to_remove.append(k)
            except Exception as exc:
                success = False
                self.logger.warn('chunk_id=%s old_fullpath=%s: %s',
                                 chunk_id, k, exc)

        # old xattr
        if raw_chunk_id is not None:
            try:
                if raw_chunk_id != chunk_id and raw_chunk_id not in links:
                    if raw_chunk_id not in new_fullpaths:
                        meta2, _ = read_chunk_metadata(fd, raw_chunk_id)
                        container_id2 = meta2['container_id'].upper()
                        path2 = meta2['content_path']
                        version2 = meta2['content_version']
                        content_id2 = meta2['content_id'].upper()

                        raw_chunk_id, new_fullpath = \
                            self.get_chunk_id_and_fullpath(
                                chunk_inode, chunk_pos, container_id2, path2,
                                version2, chunk_id=raw_chunk_id,
                                content_id=content_id2)
                        new_fullpaths[raw_chunk_id] = new_fullpath
                elif raw_chunk_id == chunk_id and fullpath is None:
                    if raw_chunk_id not in new_fullpaths:
                        raw_chunk_id, new_fullpath = \
                            self.get_chunk_id_and_fullpath(
                                chunk_inode, chunk_pos, container_id, path,
                                version, chunk_id=raw_chunk_id,
                                content_id=content_id)
                        new_fullpaths[raw_chunk_id] = new_fullpath
            except Exception as exc:
                success = False
                self.logger.warn('chunk_id=%s (old xattr): %s',
                                 raw_chunk_id, exc)

        self.save_xattr(chunk_id, raw_meta)
        # for security, if there is an error, we don't delete old xattr
        modify_xattr(fd, new_fullpaths, success, xattr_to_remove)
        return success, None

    def safe_convert_chunk(self, path, fd=None, chunk_id=None):
        if chunk_id is None:
            chunk_id = path.rsplit('/', 1)[-1]
            if len(chunk_id) != STRLEN_CHUNKID:
                self.logger.warn('Not a chunk %s' % path)
                return
            for c in chunk_id:
                if c not in hexdigits:
                    self.logger.warn('Not a chunk %s' % path)
                    return

        success = False
        self.total_chunks_processed += 1
        try:
            if fd is None:
                with open(path) as fd:
                    success, _ = self.convert_chunk(fd, chunk_id)
            else:
                success, _ = self.convert_chunk(fd, chunk_id)
        except Exception:
            self.logger.exception('ERROR while conversion %s', path)

        if not success:
            self.errors += 1
        else:
            self.logger.debug('Converted %s', path)
        self.passes += 1

    def converter_pass(self):
        def report(tag, now=None):
            if now is None:
                now = time.time()
            total_time = now - self.start_time
            self.logger.info(
                '%(tag)s  %(volume)s '
                'started=%(start_time)s '
                'passes=%(passes)d '
                'errors=%(errors)d '
                'chunks=%(nb_chunks)d %(c_rate).2f/s '
                'total_time=%(total_time).2f '
                '(converter: %(success_rate).2f%%)' % {
                    'tag': tag,
                    'volume': self.volume_id,
                    'start_time': datetime.fromtimestamp(
                        int(self.start_time)).isoformat(),
                    'passes': self.passes,
                    'errors': self.errors,
                    'nb_chunks': self.total_chunks_processed,
                    'c_rate': self.total_chunks_processed / total_time,
                    'total_time': total_time,
                    'success_rate':
                        100 * ((self.total_chunks_processed - self.errors)
                               / float(self.total_chunks_processed))
                }
            )
            self.passes = 0
            self.last_reported = now

        self.start_time = time.time()
        self.errors = 0
        self.passes = 0

        self.backup_name = 'backup_%s_%f' % (self.volume_id, self.start_time)

        paths = paths_gen(self.volume)
        for path in paths:
            self.safe_convert_chunk(path)

            now = time.time()
            if now - self.last_reported >= self.report_interval:
                report('RUN', now=now)

            self.chunks_run_time = ratelimit(self.chunks_run_time,
                                             self.max_chunks_per_second)
        report('DONE')

        return self.errors == 0