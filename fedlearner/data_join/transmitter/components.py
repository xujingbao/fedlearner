import json
import logging
import os
import queue
import threading
import typing

import fedlearner.common.common_pb2 as common_pb
import fedlearner.common.transmitter_service_pb2 as transmitter_pb
import fedlearner.common.transmitter_service_pb2_grpc as transmitter_grpc
from fedlearner.common.db_client import DBClient
from fedlearner.data_join.transmitter.utils import IDX, _EndSentinel, \
    RecvProcessException, _assign_idx_to_meta


class Sender:
    def __init__(self,
                 meta_path: str,
                 send_row_num: int,
                 file_paths: typing.List[str],
                 root_path: str = None,
                 start_from_peer: bool = False,
                 pending_len: int = 10):
        self._db_client = DBClient('dfs')
        self._meta_path = os.path.join(meta_path, 'send')
        self._send_row_num = send_row_num
        self._client = None
        # The meta is used in ACK.
        self._meta, self._has_new = self._get_meta(file_paths, root_path or '')
        # whether to start from peer's state regardless of local state
        self._start_from_peer = start_from_peer
        self._synced = False
        self._started = False
        self._finished = self._meta['finished']
        self._stopped = False
        self._request_queue = queue.Queue(pending_len)
        self._condition = threading.Condition()
        self._put_thread = threading.Thread(target=self._put_requests)
        self._send_thread = threading.Thread(target=self._send)

    @property
    def finished(self):
        with self._condition:
            return self._finished

    def _get_meta(self,
                  file_paths: list,
                  root_path: str):
        has_new = False
        meta = self._db_client.get_data(self._meta_path)
        if meta:
            meta = json.loads(meta)
            file_diff = set(file_paths) - set(meta['files'])
            if file_diff:
                # use loop to preserve path order
                for file in file_paths:
                    if file in file_diff:
                        meta['files'].append(file)
                meta['finished'] = False
                has_new = True
        else:
            meta = {
                'file_idx': 0,
                'row_idx': 0,
                'root': root_path,
                'files': file_paths,
                'finished': False
            }
            has_new = True
        self._db_client.set_data(self._meta_path, json.dumps(meta).encode())
        return meta, has_new

    def add_client(self, client: transmitter_grpc.TransmitterServiceStub):
        with self._condition:
            if not self._client:
                self._client = client

    def _sync(self):
        resp = self._client.SyncState(
            transmitter_pb.SyncRequest(has_new=self._has_new))
        with self._condition:
            # if start from peer, always start sending from peer's state
            if self._meta['file_idx'] > resp.file_idx or self._start_from_peer:
                self._send_idx = IDX(resp.file_idx,
                                     resp.row_idx)
                self._finished = False
            elif self._meta['file_idx'] == resp.file_idx \
                    and self._meta['row_idx'] > resp.row_idx:
                self._send_idx = IDX(self._meta['file_idx'],
                                     resp.row_idx)
                self._finished = False
            # else sender has staler state, so use the sender's state
            else:
                self._send_idx = IDX(self._meta['file_idx'],
                                     self._meta['row_idx'])
            self._synced = True

    def _send(self):
        if not self._synced:
            raise RuntimeError('Sender not yet synced with peer.')
        for resp in self._client.Transmit(self._request_iterator()):
            if self._stopped:
                break
            with self._condition:
                if resp.status.code == common_pb.STATUS_INVALID_REQUEST:
                    # TODO(zhangzihui): error handling
                    logging.warning(
                        '[Transmit]: STATUS_INVALID_REQUEST returned: %s', resp)
                    continue

                current_idx = IDX(self._meta['file_idx'],
                                  self._meta['row_idx'])
                resp_idx = IDX(resp.end_file_idx, resp.end_row_idx)
                forward_idx = self._resp_process(resp, current_idx)
                if resp_idx > current_idx:
                    self._meta['file_idx'] = resp_idx.file_idx
                    self._meta['row_idx'] = resp_idx.row_idx
                if resp.status.code == common_pb.STATUS_DATA_FINISHED:
                    self._meta['finished'] = True
                    self._finished = True
                    self._condition.notify_all()

                if forward_idx:
                    # if need to save state, load the state wished to save
                    current_idx = IDX(self._meta['file_idx'],
                                      self._meta['row_idx'])
                    _assign_idx_to_meta(self._meta, forward_idx)
                    self._db_client.set_data(self._meta_path,
                                             json.dumps(self._meta).encode())
                    _assign_idx_to_meta(self._meta, current_idx)

    def _put_requests(self):
        assert self._synced
        root_path = self._meta['root']
        files = self._meta['files']
        data_finished = False
        # while not data finished
        while self._send_idx.file_idx < len(self._meta['files']) \
                and not data_finished:
            if self._stopped:
                break
            # the payload to send, the IDX next to the end idx of payload,
            # and if no more data to send
            payload, end_idx, data_finished = self._send_process(
                root_path, files, self._send_idx, self._send_row_num)
            if data_finished:
                status = common_pb.Status(code=common_pb.STATUS_DATA_FINISHED)
            else:
                status = common_pb.Status(code=common_pb.STATUS_SUCCESS)
            req = transmitter_pb.Request(status=status,
                                         start_file_idx=self._send_idx.file_idx,
                                         start_row_idx=self._send_idx.row_idx,
                                         end_file_idx=end_idx.file_idx,
                                         end_row_idx=end_idx.row_idx,
                                         payload=payload)

            self._send_idx = end_idx
            # Queue will block this thread if no slot available.
            self._request_queue.put(req)
        else:
            # put a sentinel to tell iterator to stop
            self._request_queue.put(_EndSentinel())

    def _request_iterator(self):
        while not self._finished and not self._stopped:
            # use timeout to check condition rather than blocking continuously.
            # Queue object is thread-safe, no need to use Lock.
            try:
                req = self._request_queue.get(timeout=5)
                if isinstance(req, _EndSentinel):
                    break
                yield req
            except queue.Empty:
                pass

    def start(self):
        with self._condition:
            assert self._client
            if not self._started:
                self._sync()
                self._put_thread.start()
                self._send_thread.start()
                self._started = True

    def wait_for_finish(self):
        if not self.finished and not self._stopped:
            with self._condition:
                while not self.finished and not self._stopped:
                    self._condition.wait()

    def stop(self, *args, **kwargs):
        with self._condition:
            if not self._stopped:
                self._put_thread.join()
                self._send_thread.join()
                self._stop(*args, **kwargs)
                self._stopped = True
                self._condition.notify_all()

    def _stop(self, *args, **kwargs):
        """
        Called before stopping, for shutting down custom objects like dumper.
            No need to inherit if nothing needs to be done at exit.
        Returns:

        """
        pass

    def _send_process(self,
                      root_path: str,
                      files: typing.List[str],
                      start_idx: IDX,
                      row_num: int) -> (bytes, IDX, bool):
        """
        This method handles file reading and returns the payload.
        Args:
            root_path: the root path of files.
            files: all files to be sent.
            start_idx: an IDX indicating from where the request should start.
            row_num: suggested num of lines to read.

        Returns:
            a payload bytes,
            an IDX indicating the start of next request, i.e., next to the end
                row of this request. Be aware of file finished,
            a bool indicating whether all of the data have been read.
        """
        raise NotImplementedError('_send_process not implemented.')

    def _resp_process(self,
                      resp: transmitter_pb.Response,
                      current_idx: IDX) -> IDX:
        """
        This method handles the response returned by server. Return True if
            nothing is needed to do.
        Args:
            resp: a st_pb.Response to handle.
            current_idx: a IDX indicating the progress of transmit.

        NOTE: Channel will return response in the original order of send
            sequence, but peer might start from a former index(after restart),
            so it is possible to have duplicate response.

        Returns:
            an IDX indicating where the state on disk should be updated to.
                Return None if no need to save a new state. Note that this IDX
                may be staler than meta's, meaning that some part of this resp
                is fully processed and the remaining part is still processing.
        """
        raise NotImplementedError('_resp_process not implemented.')


class Receiver:
    def __init__(self,
                 meta_path: str,
                 output_path: str):
        self._db_client = DBClient('dfs')
        self._meta_path = os.path.join(meta_path, 'recv')
        self._meta = self._get_meta(output_path)
        self._output_path = self._meta['output_path']
        self._finished = self._meta['finished']
        self._condition = threading.Condition()
        self._stopped = False

    @property
    def finished(self) -> bool:
        with self._condition:
            return self._finished

    def _get_meta(self, output_path: str) -> dict:
        meta = self._db_client.get_data(self._meta_path)
        if meta:
            meta = json.loads(meta)
        else:
            meta = {
                'output_path': output_path,
                'file_idx': 0,
                'row_idx': 0,
                'finished': False
            }
            self._db_client.set_data(self._meta_path, json.dumps(meta).encode())
        return meta

    def wait_for_finish(self):
        if not self._finished and not self._stopped:
            with self._condition:
                while not self._finished and not self._stopped:
                    self._condition.wait()

    def stop(self, *args, **kwargs):
        with self._condition:
            if not self._stopped:
                self._stop(*args, **kwargs)
                self._stopped = True
                self._condition.notify_all()

    def sync_state(self, request):
        with self._condition:
            if request.has_new:
                self._finished = False
                self._meta['finished'] = False
                # not dump current meta b/c it may differ with the meta on disk
                meta = json.loads(self._db_client.get_data(self._meta_path))
                meta['finished'] = False
                self._db_client.set_data(self._meta_path,
                                         json.dumps(meta).encode())
            return transmitter_pb.SyncResponse(file_idx=self._meta['file_idx'],
                                               row_idx=self._meta['row_idx'])

    def transmit(self, request_iterator: typing.Iterable):
        for r in request_iterator:
            if self._stopped:
                break
            if self._finished:
                raise RuntimeError('The stream has already finished.')
            with self._condition:
                # Channel assures us there is a subsequence of requests that
                #   constitutes the original requests sequence, including order,
                #   so we need to deal with preceded and duplicated requests.
                yield self._process_request(r)
        else:
            # only finish when all requests have been processed, as sender may
            #   miss some responses and send some duplicates
            with self._condition:
                self._meta['finished'] = True
                self._finished = True
                self._condition.notify_all()
                self._db_client.set_data(self._meta_path,
                                         json.dumps(self._meta))

    def _process_request(self,
                         req: transmitter_pb.Request):
        with self._condition:
            try:
                current_idx = IDX(self._meta['file_idx'], self._meta['row_idx'])
                end_idx = IDX(req.end_file_idx, req.end_row_idx)
                payload, forward_idx = self._recv_process(req, current_idx)
            except RecvProcessException as e:
                return transmitter_pb.Response(
                    status=common_pb.Status(
                        code=common_pb.STATUS_INVALID_REQUEST,
                        error_message=repr(e)))
            # status is good from here as bad status has been returned
            if current_idx < end_idx:
                # if newer, update meta
                _assign_idx_to_meta(self._meta, end_idx)
            if forward_idx:
                current_idx = IDX(self._meta['file_idx'],
                                  self._meta['row_idx'])
                _assign_idx_to_meta(self._meta, forward_idx)
                self._db_client.set_data(self._meta_path,
                                         json.dumps(self._meta).encode())
                _assign_idx_to_meta(self._meta, current_idx)

            status = common_pb.Status()
            status.MergeFrom(req.status)
            return transmitter_pb.Response(status=status,
                                           start_file_idx=req.start_file_idx,
                                           start_row_idx=req.start_row_idx,
                                           end_file_idx=req.end_file_idx,
                                           end_row_idx=req.end_row_idx,
                                           payload=payload)

    def _stop(self, *args, **kwargs):
        """
        Called before stopping, for shutting down custom objects like dumper.
            No need to inherit if nothing needs to be done at exit.
        Returns:

        """
        pass

    def _recv_process(self,
                      req: transmitter_pb.Request,
                      current_idx: IDX) -> (bytes, IDX):
        """
        This method should handle preceded and duplicated requests properly,
            and NOTE THAT SENDER MAY SEND DUPLICATED REQUESTS EVEN WHEN THE
            RECEIVER IS FINISHED, as the sender might not receive the response.

        Args:
            req: a request pb
            current_idx: an IDX indicating the current transmit progress.

        Returns:
            a payload bytes string.
            an IDX indicating where the meta should be updated to and saved.
                Return None if no need to save state.

        NOTE:
            after transmit finishes, meta will be automatically saved regardless
                of the IDX returned here.
        """
        raise NotImplementedError('_receive_process not implemented.')