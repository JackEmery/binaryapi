"""Module for Binary API."""
from typing import Optional, Any, Union, Callable
from collections import OrderedDict, deque
from threading import Thread
import logging
import decimal
import typing
import ssl
import time

import simplejson as json

from binaryapi import global_value
from binaryapi import global_config
from binaryapi.utils import nested_dict
from binaryapi.ws.abstract import AbstractAPI
from binaryapi.ws.client import WebsocketClient
from binaryapi.utils.memory_footprint import total_size
from binaryapi.exceptions import MessageByReqIDNotFound

from binaryapi.ws.objects.authorize import Authorize as AuthorizeObject

DEFAULT_RESPONSE_TIMEOUT: Union[int, float] = 30
DEFAULT_RESPONSE_CHECK_DELAY: Union[int, float] = 0.5 / 1000
AUTHORIZE_MAX_TIMEOUT: Union[int, float] = 30
DEFAULT_MAX_DICT_SIZE: int = 500
DEFAULT_MAX_SUBSCRIPTION_MSG_LIST_SIZE = 100
DEFAULT_APP_ID: int = 28035


class FixSizeOrderedDict(OrderedDict):
    # noinspection PyShadowingBuiltins
    def __init__(self, *args, max=0, **kwargs):
        self._max = max
        super().__init__(*args, **kwargs)

    def __setitem__(self, key, value):
        OrderedDict.__setitem__(self, key, value)
        if self._max > 0:
            if len(self) > self._max:
                self.popitem(False)


class BinaryAPI(AbstractAPI):
    websocket_thread: Thread
    websocket_client: Optional[WebsocketClient]
    profile: AuthorizeObject

    message_callback: Optional[Callable]

    # results: typing.OrderedDict[int, Any]
    msg_by_req_id: typing.OrderedDict[int, Any]
    msg_by_type: typing.DefaultDict[str, typing.OrderedDict[int, Any]]
    _request_id: int

    subscriptions: typing.OrderedDict[str, str]  # Subscription ID -> MSG Type
    msg_by_subscription: typing.DefaultDict[str, typing.Deque]  # Subscription ID -> List of messages in ascending order

    def __init__(self, token: str = None, app_id: int = None):
        self.app_id = app_id or DEFAULT_APP_ID
        self.token = token

        self.wss_url = "wss://ws.binaryws.com/websockets/v3?app_id={0}".format(self.app_id)

        self.message_callback = None
        self.websocket_client = None

        self.profile = AuthorizeObject()
        # self.results = FixSizeOrderedDict(max=DEFAULT_MAX_DICT_SIZE)
        self.msg_by_req_id = FixSizeOrderedDict(max=DEFAULT_MAX_DICT_SIZE)
        self.msg_by_type = nested_dict(1, lambda: FixSizeOrderedDict(max=DEFAULT_MAX_DICT_SIZE))

        self.subscriptions = OrderedDict()
        self.msg_by_subscription = nested_dict(1, lambda: deque(maxlen=DEFAULT_MAX_SUBSCRIPTION_MSG_LIST_SIZE))

        self._request_id = 1

    def connect(self):
        global_value.check_websocket_if_connect = None

        if global_config.AUTO_CLEAR_VAR_SUBSCRIPTIONS_ON_CONNECT:
            self.subscriptions.clear()

        if global_config.AUTO_CLEAR_VAR_MSG_BY_SUBSCRIPTION_ON_CONNECT:
            self.msg_by_subscription.clear()


        # TODO clear more cached messages

        self.websocket_client = WebsocketClient(self)

        # noinspection SpellCheckingInspection
        self.websocket_thread = Thread(
            target=self.websocket.run_forever,
            kwargs={
                'sslopt': {
                    "check_hostname": False, "cert_reqs": ssl.CERT_NONE,
                    "ca_certs": "cacert.pem"
                },
                "ping_interval": 5,
            }
        )  # for fix pyinstall error: cafile, capath and cadata cannot be all omitted
        self.websocket_thread.daemon = True
        self.websocket_thread.start()

        while True:
            # noinspection PyBroadException
            try:
                if global_value.check_websocket_if_connect in [0, -1]:
                    return False
                elif global_value.check_websocket_if_connect == 1:
                    break
            except Exception:
                pass

        if self.token:
            auth_req_id = self.authorize(authorize=self.token)

            try:
                self.wait_for_response_by_req_id(req_id=auth_req_id, type='authorize', max_timeout=AUTHORIZE_MAX_TIMEOUT, delay=0.01, error_label='error')
            except MessageByReqIDNotFound:
                return False

        # start_t = time.time()
        # while self.profile.msg is None:
        #     if time.time() - start_t >= 30:
        #         logging.error('**error** authorize late 30 sec')
        #         return False
        #
        #     pause.seconds(0.01)

        return True

    @property
    def websocket(self):
        """Property to get websocket.
        :returns: The instance of :class:`WebSocket <websocket.WebSocket>`.
        """
        return self.websocket_client.wss

    def close(self):
        self.websocket.close()
        self.websocket_thread.join()

    def websocket_alive(self):
        return self.websocket_thread.is_alive()

    @property
    def request_id(self):
        self._request_id += 1
        return self._request_id - 1

    # noinspection PyShadowingBuiltins
    def wait_for_response_by_req_id(
        self,
        req_id: int,
        type: Optional[str] = None,
        type_name: Optional[str] = None,
        max_timeout: Union[int, float] = DEFAULT_RESPONSE_TIMEOUT,
        delay: Union[int, float] = DEFAULT_RESPONSE_CHECK_DELAY,
        error_label: str = 'warning'
    ):
        start_time = time.time()

        type_name_repr: Optional[str] = type_name or type
        while (self.msg_by_req_id.get(req_id) is None) if type is None else (self.msg_by_type[type].get(req_id) is None):
            if time.time() - start_time >= max_timeout:
                logging.error('**{}** {}late {} sec(s)'.format(error_label, max_timeout, (type_name_repr + ' ') if type_name_repr else ''))
                # return False, None, request_id
                raise MessageByReqIDNotFound
            time.sleep(delay)

        return

    # noinspection PyShadowingBuiltins
    def get_response_by_req_id(
            self,
            req_id: int,
            type: Optional[str] = None,
            **kwargs
    ):
        self.wait_for_response_by_req_id(req_id=req_id, type=type, **kwargs)
        return self.msg_by_req_id.get(req_id) if type is None else self.msg_by_type[type].get(req_id)

    wait_for_msg_by_req_id = wait_for_response_by_req_id
    get_msg_by_req_id = get_response_by_req_id

    def send_websocket_request(
            self,
            name: str,
            msg: Any,
            passthrough:
            Optional[Any] = None,
            req_id: int = None
    ) -> int:
        """Send websocket request to Binary server.
        :type passthrough: dict
        :type name: str
        :param req_id: int
        :param dict msg: The websocket request msg.
        """
        logger = logging.getLogger(__name__)

        if req_id is None:
            req_id = self.request_id

        if req_id:
            msg['req_id'] = req_id

            # Delete any data with same req_id
            # self.results[req_id] = None
            self.msg_by_req_id[req_id] = None
            self.msg_by_type[name][req_id] = None

        if passthrough:
            msg["passthrough"] = passthrough

        def default(obj):
            if isinstance(obj, decimal.Decimal):
                return str(obj)
            raise TypeError

        data = json.dumps(msg, default=default)

        self.websocket.send(data)

        logger.debug(data)

        return req_id

    def print_memory_footprint(self):
        # This function is not 100% accurate but should give a hint about memory usage. (this function is subject to change)
        # verbose: bool = True
        # total_size(self, verbose=verbose)
        object_names = [
            'msg_by_req_id', 'msg_by_type', 'subscriptions', 'msg_by_subscription',
            'self'
        ]

        for object_name in object_names:
            # noinspection PyBroadException
            try:
                obj: Any
                if object_name == 'self':
                    obj = self
                else:
                    obj = getattr(self, object_name)
                size = total_size(obj)
                print(object_name, '->', size, 'byte(s)')
            except Exception:
                pass


DerivAPI = BinaryAPI
