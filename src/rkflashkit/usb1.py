"""
Pythonic wrapper for libusb-1.0.

The first thing you must do is to get an "USB context". To do so, create an
USBContext instance.
Then, you can use it to browse available USB devices and open the one you want
to talk to.
At this point, you should have a USBDeviceHandle instance (as returned by
USBContext or USBDevice instances), and you can start exchanging with the
device.

Features:
- Basic device settings (configuration & interface selection, ...)
- String descriptor lookups (ASCII & unicode), and list supported language
  codes
- Synchronous I/O (control, bulk, interrupt)
- Asynchronous I/O (control, bulk, interrupt, isochronous)
  Note: Isochronous support is not well tested.
  See USBPoller, USBTransfer and USBTransferHelper.
"""

import libusb1
from ctypes import byref, create_string_buffer, c_int, sizeof, POINTER, \
    cast, c_uint16, c_ubyte, string_at, c_void_p, cdll
import sys
import threading
from ctypes.util import find_library
import warnings
import weakref
import collections
try:
    namedtuple = collections.namedtuple
except AttributeError:
    Version = lambda *x: x
else:
    Version = namedtuple('Version', ['major', 'minor', 'micro', 'nano', 'rc',
        'describe'])

if sys.version_info[0] == 3:
    BYTE = bytes([0])
    xrange = range
    long = int
else:
    BYTE = '\x00'
CONTROL_SETUP = BYTE * libusb1.LIBUSB_CONTROL_SETUP_SIZE

__all__ = ['USBContext', 'USBDeviceHandle', 'USBDevice',
    'USBPoller', 'USBTransfer', 'USBTransferHelper', 'EVENT_CALLBACK_SET',
    'USBPollerThread', 'USBEndpoint', 'USBInterfaceSetting', 'USBInterface',
    'USBConfiguration', 'DoomedTransferError', 'getVersion',
]

if sys.version_info[:2] >= (2, 6):
    if sys.platform == 'win32':
        from ctypes import get_last_error as get_errno
    else:
        from ctypes import get_errno
else:
    def get_errno():
        raise NotImplementedError("Your python version doesn't support "
            "errno/last_error")

__libc_name = find_library('c')
if __libc_name is None:
    # Of course, will leak memory.
    # Should we warn user ? How ?
    _free = lambda x: None
else:
    _free = getattr(cdll, __libc_name).free
del __libc_name

try:
    WeakSet = weakref.WeakSet
except AttributeError:
    # Python < 2.7: tiny wrapper around WeakKeyDictionary
    class WeakSet(object):
        def __init__(self):
            self.__dict = weakref.WeakKeyDictionary()

        def add(self, item):
            self.__dict[item] = None

        def pop(self):
            return self.__dict.popitem()[0]

# Default string length
# From a comment in libusb-1.0: "Some devices choke on size > 255"
STRING_LENGTH = 255

EVENT_CALLBACK_SET = frozenset((
    libusb1.LIBUSB_TRANSFER_COMPLETED,
    libusb1.LIBUSB_TRANSFER_ERROR,
    libusb1.LIBUSB_TRANSFER_TIMED_OUT,
    libusb1.LIBUSB_TRANSFER_CANCELLED,
    libusb1.LIBUSB_TRANSFER_STALL,
    libusb1.LIBUSB_TRANSFER_NO_DEVICE,
    libusb1.LIBUSB_TRANSFER_OVERFLOW,
))

DEFAULT_ASYNC_TRANSFER_ERROR_CALLBACK = lambda x: False

def create_binary_buffer(string_or_len):
    # Prevent ctypes from adding a trailing null char.
    if isinstance(string_or_len, (int, long)):
        result = create_string_buffer(string_or_len)
    else:
        result = create_string_buffer(string_or_len, len(string_or_len))
    return result

class DoomedTransferError(Exception):
    """Exception raised when altering/submitting a doomed transfer."""
    pass

class USBTransfer(object):
    """
    USB asynchronous transfer control & data.

    All modification methods will raise if called on a submitted transfer.
    Methods noted as "should not be called on a submitted transfer" will not
    prevent you from reading, but returned value is unspecified.

    Note on user_data: because of pypy's current ctype restrictions, user_data
    is not provided to C level, but is managed purely in python. It should
    change nothing for you, unless you are looking at underlying C transfer
    structure - which you should never have to.
    """
    # Prevent garbage collector from freeing the free function before our
    # instances, as we need it to property destruct them.
    __libusb_free_transfer = libusb1.libusb_free_transfer
    __libusb_cancel_transfer = libusb1.libusb_cancel_transfer
    __USBError = libusb1.USBError
    __LIBUSB_ERROR_NOT_FOUND = libusb1.LIBUSB_ERROR_NOT_FOUND
    __transfer = None
    __initialized = False
    __submitted = False
    __callback = None
    __ctypesCallbackWrapper = None
    __doomed = False
    __user_data = None
    __transfer_buffer = None

    def __init__(self, handle, iso_packets, before_submit, after_completion):
        """
        You should not instanciate this class directly.
        Call "getTransfer" method on an USBDeviceHandle instance to get
        instances of this class.
        """
        if iso_packets < 0:
            raise ValueError('Cannot request a negative number of iso '
                'packets.')
        self.__handle = handle
        self.__before_submit = before_submit
        self.__after_completion = after_completion
        self.__num_iso_packets = iso_packets
        result = libusb1.libusb_alloc_transfer(iso_packets)
        if not result:
            raise libusb1.USBError('Unable to get a transfer object')
        self.__transfer = result
        self.__ctypesCallbackWrapper = libusb1.libusb_transfer_cb_fn_p(
            self.__callbackWrapper)

    def close(self):
        """
        Break reference cycles to allow instance to be garbage-collected.
        Raises if called on a submitted transfer.
        """
        if self.__submitted:
            raise ValueError('Cannot close a submitted transfer')
        self.doom()
        self.__initialized = False
        # Break possible external reference cycle
        self.__callback = None
        # Break libusb_transfer reference cycles
        self.__ctypesCallbackWrapper = None
        # For some reason, overwriting callback is not enough to remove this
        # reference cycle - though sometimes it works:
        #   self -> self.__dict__ -> libusb_transfer -> dict[x] -> dict[x] ->
        #   CThunkObject -> __callbackWrapper -> self
        # So free transfer altogether.
        if self.__transfer is not None:
            self.__libusb_free_transfer(self.__transfer)
            self.__transfer = None
        # Break USBDeviceHandle reference cycle
        self.__before_submit = None
        self.__after_completion = None

    def doom(self):
        """
        Prevent transfer from being submitted again.
        """
        self.__doomed = True

    def __del__(self):
        if self.__transfer is not None:
            try:
                # If this doesn't raise, we're doomed; transfer was submitted,
                # still python decided to garbage-collect this instance.
                # Stick to libusb's documentation, and don't free the
                # transfer. If interpreter is shutting down, kernel will
                # reclaim memory anyway.
                # Note: we can't prevent transfer's buffer from being
                # garbage-collected as soon as there will be no remaining
                # reference to transfer, so a segfault might happen anyway.
                # Should we warn user ? How ?
                self.cancel()
            except self.__USBError:
                if sys.exc_info()[1].value == self.__LIBUSB_ERROR_NOT_FOUND:
                    # Transfer was not submitted, we can free it.
                    self.__libusb_free_transfer(self.__transfer)
                else:
                    raise

    def __callbackWrapper(self, transfer_p):
        """
        Makes it possible for user-provided callback to alter transfer when
        fired (ie, mark transfer as not submitted upon call).
        """
        self.__submitted = False
        self.__after_completion(self)
        callback = self.__callback
        if callback is not None:
            callback(self)
        if self.__doomed:
            self.close()

    def setCallback(self, callback):
        """
        Change transfer's callback.
        """
        self.__callback = callback

    def getCallback(self):
        """
        Get currently set callback.
        """
        return self.__callback

    def setControl(self, request_type, request, value, index, buffer_or_len,
            callback=None, user_data=None, timeout=0):
        """
        Setup transfer for control use.

        request_type, request, value, index
            See USBDeviceHandle.controlWrite.
            request_type defines transfer direction (see
            libusb1.LIBUSB_ENDPOINT_OUT and libusb1.LIBUSB_ENDPOINT_IN)).
        buffer_or_len
            Either a string (when sending data), or expected data length (when
            receiving data).
        callback
            Callback function to be invoked on transfer completion.
            Called with transfer as parameter, return value ignored.
        user_data
            User data to pass to callback function.
        timeout
            Transfer timeout in milliseconds. 0 to disable.
        """
        if self.__submitted:
            raise ValueError('Cannot alter a submitted transfer')
        if self.__doomed:
            raise DoomedTransferError('Cannot reuse a doomed transfer')
        if isinstance(buffer_or_len, (int, long)):
            length = buffer_or_len
            string_buffer = create_binary_buffer(length + \
                libusb1.LIBUSB_CONTROL_SETUP_SIZE)
        else:
            length = len(buffer_or_len)
            string_buffer = create_binary_buffer(CONTROL_SETUP + buffer_or_len)
        self.__initialized = False
        self.__transfer_buffer = string_buffer
        self.__user_data = user_data
        libusb1.libusb_fill_control_setup(string_buffer, request_type,
            request, value, index, length)
        libusb1.libusb_fill_control_transfer(self.__transfer, self.__handle,
            string_buffer, self.__ctypesCallbackWrapper, None, timeout)
        self.__callback = callback
        self.__initialized = True

    def setBulk(self, endpoint, buffer_or_len, callback=None, user_data=None,
            timeout=0):
        """
        Setup transfer for bulk use.

        endpoint
            Endpoint to submit transfer to. Defines transfer direction (see
            libusb1.LIBUSB_ENDPOINT_OUT and libusb1.LIBUSB_ENDPOINT_IN)).
        buffer_or_len
            Either a string (when sending data), or expected data length (when
            receiving data)
        callback
            Callback function to be invoked on transfer completion.
            Called with transfer as parameter, return value ignored.
        user_data
            User data to pass to callback function.
        timeout
            Transfer timeout in milliseconds. 0 to disable.
        """
        if self.__submitted:
            raise ValueError('Cannot alter a submitted transfer')
        if self.__doomed:
            raise DoomedTransferError('Cannot reuse a doomed transfer')
        string_buffer = create_binary_buffer(buffer_or_len)
        self.__initialized = False
        self.__transfer_buffer = string_buffer
        self.__user_data = user_data
        libusb1.libusb_fill_bulk_transfer(self.__transfer, self.__handle,
            endpoint, string_buffer, sizeof(string_buffer),
            self.__ctypesCallbackWrapper, None, timeout)
        self.__callback = callback
        self.__initialized = True

    def setInterrupt(self, endpoint, buffer_or_len, callback=None,
            user_data=None, timeout=0):
        """
        Setup transfer for interrupt use.

        endpoint
            Endpoint to submit transfer to. Defines transfer direction (see
            libusb1.LIBUSB_ENDPOINT_OUT and libusb1.LIBUSB_ENDPOINT_IN)).
        buffer_or_len
            Either a string (when sending data), or expected data length (when
            receiving data)
        callback
            Callback function to be invoked on transfer completion.
            Called with transfer as parameter, return value ignored.
        user_data
            User data to pass to callback function.
        timeout
            Transfer timeout in milliseconds. 0 to disable.
        """
        if self.__submitted:
            raise ValueError('Cannot alter a submitted transfer')
        if self.__doomed:
            raise DoomedTransferError('Cannot reuse a doomed transfer')
        string_buffer = create_binary_buffer(buffer_or_len)
        self.__initialized = False
        self.__transfer_buffer = string_buffer
        self.__user_data = user_data
        libusb1.libusb_fill_interrupt_transfer(self.__transfer, self.__handle,
            endpoint, string_buffer,  sizeof(string_buffer),
            self.__ctypesCallbackWrapper, None, timeout)
        self.__callback = callback
        self.__initialized = True

    def setIsochronous(self, endpoint, buffer_or_len, callback=None,
            user_data=None, timeout=0, iso_transfer_length_list=None):
        """
        Setup transfer for isochronous use.

        endpoint
            Endpoint to submit transfer to. Defines transfer direction (see
            libusb1.LIBUSB_ENDPOINT_OUT and libusb1.LIBUSB_ENDPOINT_IN)).
        buffer_or_len
            Either a string (when sending data), or expected data length (when
            receiving data)
        callback
            Callback function to be invoked on transfer completion.
            Called with transfer as parameter, return value ignored.
        user_data
            User data to pass to callback function.
        timeout
            Transfer timeout in milliseconds. 0 to disable.
        iso_transfer_length_list
            List of individual transfer sizes. If not provided, buffer_or_len
            will be divided evenly among available transfers if possible, and
            raise ValueError otherwise.
        """
        if self.__submitted:
            raise ValueError('Cannot alter a submitted transfer')
        num_iso_packets = self.__num_iso_packets
        if num_iso_packets == 0:
            raise TypeError('This transfer canot be used for isochronous I/O. '
                'You must get another one with a non-zero iso_packets '
                'parameter.')
        if self.__doomed:
            raise DoomedTransferError('Cannot reuse a doomed transfer')
        string_buffer = create_binary_buffer(buffer_or_len)
        buffer_length = sizeof(string_buffer)
        if iso_transfer_length_list is None:
            iso_length, remainder = divmod(buffer_length, num_iso_packets)
            if remainder:
                raise ValueError('Buffer size %i cannot be evenly '
                    'distributed among %i transfers' % (buffer_length,
                    num_iso_packets))
            iso_transfer_length_list = [iso_length] * num_iso_packets
        configured_iso_packets = len(iso_transfer_length_list)
        if configured_iso_packets > num_iso_packets:
            raise ValueError('Too many ISO transfer lengths (%i), there are '
                'only %i ISO transfers available' % (configured_iso_packets,
                    num_iso_packets))
        if sum(iso_transfer_length_list) > buffer_length:
            raise ValueError('ISO transfers too long (%i), there are only '
                '%i bytes available' % (sum(iso_transfer_length_list),
                    buffer_length))
        transfer_p = self.__transfer
        self.__initialized = False
        self.__transfer_buffer = string_buffer
        self.__user_data = user_data
        libusb1.libusb_fill_iso_transfer(transfer_p, self.__handle,
            endpoint, string_buffer, buffer_length, configured_iso_packets,
            self.__ctypesCallbackWrapper, None, timeout)
        for length, iso_packet_desc in zip(iso_transfer_length_list,
                libusb1.get_iso_packet_list(transfer_p)):
            if length <= 0:
                raise ValueError('Negative/null length transfers are not '
                    'possible.')
            iso_packet_desc.length = length
        self.__callback = callback
        self.__initialized = True

    def getType(self):
        """
        Get transfer type.
        See libusb1.libusb_transfer_type.
        """
        return self.__transfer.contents.type

    def getEndpoint(self):
        """
        Get endpoint.
        """
        return self.__transfer.contents.endpoint

    def getStatus(self):
        """
        Get transfer status.
        Should not be called on a submitted transfer.
        """
        return self.__transfer.contents.status

    def getActualLength(self):
        """
        Get actually transfered data length.
        Should not be called on a submitted transfer.
        """
        return self.__transfer.contents.actual_length

    def getBuffer(self):
        """
        Get data buffer content.
        Should not be called on a submitted transfer.
        """
        transfer_p = self.__transfer
        transfer = transfer_p.contents
        if transfer.type == libusb1.LIBUSB_TRANSFER_TYPE_CONTROL:
            result = libusb1.libusb_control_transfer_get_data(transfer_p)
        else:
            result = string_at(transfer.buffer, transfer.length)
        return result

    def getUserData(self):
        """
        Retrieve user data provided on setup.
        """
        return self.__user_data

    def setUserData(self, user_data):
        """
        Change user data.
        """
        self.__user_data = user_data

    def getISOBufferList(self):
        """
        Get individual ISO transfer's buffer.
        Returns a list with one item per ISO transfer, with their
        individually-configured sizes.
        Returned list is consistent with getISOSetupList return value.
        Should not be called on a submitted transfer.
        """
        transfer_p = self.__transfer
        transfer = transfer_p.contents
        if transfer.type != libusb1.LIBUSB_TRANSFER_TYPE_ISOCHRONOUS:
            raise TypeError('This method cannot be called on non-iso '
                'transfers.')
        return libusb1.get_iso_packet_buffer_list(transfer_p)

    def getISOSetupList(self):
        """
        Get individual ISO transfer's setup.
        Returns a list of dicts, each containing an individual ISO transfer
        parameters:
        - length
        - actual_length
        - status
        (see libusb1's API documentation for their signification)
        Returned list is consistent with getISOBufferList return value.
        Should not be called on a submitted transfer (except for 'length'
        values).
        """
        transfer_p = self.__transfer
        transfer = transfer_p.contents
        if transfer.type != libusb1.LIBUSB_TRANSFER_TYPE_ISOCHRONOUS:
            raise TypeError('This method cannot be called on non-iso '
                'transfers.')
        return [{
                'length': x.length,
                'actual_length': x.actual_length,
                'status': x.status,
            } for x in libusb1.get_iso_packet_list(transfer_p)]

    def setBuffer(self, buffer_or_len):
        """
        Replace buffer with a new one.
        Allows resizing read buffer and replacing data sent.
        Note: resizing is not allowed for isochronous buffer (use
        setIsochronous).
        Note: disallowed on control transfers (use setControl).
        """
        if self.__submitted:
            raise ValueError('Cannot alter a submitted transfer')
        transfer = self.__transfer.contents
        if transfer.type == libusb1.LIBUSB_TRANSFER_TYPE_CONTROL:
            raise ValueError('To alter control transfer buffer, use '
                'setControl')
        buff = create_binary_buffer(buffer_or_len)
        if transfer.type == libusb1.LIBUSB_TRANSFER_TYPE_ISOCHRONOUS and \
                sizeof(buff) != transfer.length:
            raise ValueError('To alter isochronous transfer buffer length, '
                'use setIsochronous')
        self.__transfer_buffer = buff
        transfer.buffer = cast(buff, c_void_p)
        transfer.length = sizeof(buff)

    def isSubmitted(self):
        """
        Tells if this transfer is submitted and still pending.
        """
        return self.__submitted

    def submit(self):
        """
        Submit transfer for asynchronous handling.
        """
        if self.__submitted:
            raise ValueError('Cannot submit a submitted transfer')
        if not self.__initialized:
            raise ValueError('Cannot submit a transfer until it has been '
                'initialized')
        if self.__doomed:
            raise DoomedTransferError('Cannot submit doomed transfer')
        self.__before_submit(self)
        self.__submitted = True
        result = libusb1.libusb_submit_transfer(self.__transfer)
        if result:
            self.__after_completion(self)
            self.__submitted = False
            raise libusb1.USBError(result)

    def cancel(self):
        """
        Cancel transfer.
        Note: cancellation happens asynchronously, so you must wait for
        LIBUSB_TRANSFER_CANCELLED.
        """
        if not self.__submitted:
            # XXX: Workaround for a bug reported on libusb 1.0.8: calling
            # libusb_cancel_transfer on a non-submitted transfer might
            # trigger a segfault.
            raise self.__USBError(self.__LIBUSB_ERROR_NOT_FOUND)
        result = self.__libusb_cancel_transfer(self.__transfer)
        if result:
            raise self.__USBError(result)

class USBTransferHelper(object):
    """
    Simplifies subscribing to the same transfer over and over, and callback
    handling:
    - no need to read event status to execute apropriate code, just setup
      different functions for each status code
    - just return True instead of calling submit
    - no need to check if transfer is doomed before submitting it again,
      DoomedTransferError is caught.

    Callbacks used in this class must follow the callback API described in
    USBTransfer, and are expected to return a boolean:
    - True if transfer is to be submitted again (to receive/send more data)
    - False otherwise

    Note: as per libusb1 specifications, isochronous transfer global state
    might be LIBUSB_TRANSFER_COMPLETED although some individual packets might
    have an error status. You can check individual packet status by calling
    getISOSetupList on transfer object in your callback.
    """
    def __init__(self, transfer=None):
        """
        Create a transfer callback dispatcher.

        transfer parameter is deprecated. If provided, it will be equivalent
        to:
            helper = USBTransferHelper()
            transfer.setCallback(helper)
        and also allows using deprecated methods on this class (otherwise,
        they raise AttributeError).
        """
        if transfer is not None:
            # Deprecated: to drop
            self.__transfer = transfer
            transfer.setCallback(self)
        self.__event_callback_dict = {}
        self.__errorCallback = DEFAULT_ASYNC_TRANSFER_ERROR_CALLBACK

    def submit(self):
        """
        Submit the asynchronous read request.
        Deprecated. Use submit on transfer.
        """
        # Deprecated: to drop
        self.__transfer.submit()

    def cancel(self):
        """
        Cancel a pending read request.
        Deprecated. Use cancel on transfer.
        """
        # Deprecated: to drop
        self.__transfer.cancel()

    def setEventCallback(self, event, callback):
        """
        Set a function to call for a given event.
        Possible event identifiers are listed in EVENT_CALLBACK_SET.
        """
        if event not in EVENT_CALLBACK_SET:
            raise ValueError('Unknown event %r.' % (event, ))
        self.__event_callback_dict[event] = callback

    def setDefaultCallback(self, callback):
        """
        Set the function to call for event which don't have a specific callback
        registered.
        The initial default callback does nothing and returns False.
        """
        self.__errorCallback = callback

    def getEventCallback(self, event, default=None):
        """
        Return the function registered to be called for given event identifier.
        """
        return self.__event_callback_dict.get(event, default)

    def __call__(self, transfer):
        """
        Callback to set on transfers.
        """
        if self.getEventCallback(transfer.getStatus(), self.__errorCallback)(
                transfer):
            try:
                transfer.submit()
            except DoomedTransferError:
                pass

    def isSubmited(self):
        """
        Returns whether this reader is currently waiting for an event.
        Deprecatd. Use isSubmitted on transfer.
        """
        # Deprecated: to drop
        return self.__transfer.isSubmitted()

class USBPollerThread(threading.Thread):
    """
    Implements libusb1 documentation about threaded, asynchronous
    applications.
    In short, instanciate this class once (...per LibUSBContext instance), call
    start() on the instance, and do whatever you need.
    This thread will be used to execute transfer completion callbacks, and you
    are free to use libusb1's synchronous API in another thread, and can forget
    about libusb1 file descriptors.

    See http://libusb.sourceforge.net/api-1.0/mtasync.html .
    """
    def __init__(self, context, poller, exc_callback=None):
        """
        Create a poller thread for given context.
        Warning: it will not check if another poller instance was already
        present for that context, and will replace it.

        poller
            (same as USBPoller.__init__ "poller" parameter)

        exc_callback (callable)
          Called with a libusb_error value as single parameter when event
          handling fails.
          If not given, an USBError will be raised, interrupting the thread.
        """
        super(USBPollerThread, self).__init__()
        self.daemon = True
        self.__context = context
        self.__poller = poller
        self.__fd_set = set()
        context.setPollFDNotifiers(self._registerFD, self._unregisterFD)
        for fd, events in context.getPollFDList():
            self._registerFD(fd, events, None)
        if exc_callback is not None:
            self.exceptionHandler = exc_callback

    def __del__(self):
        self.__context.setPollFDNotifiers(None, None)

    @staticmethod
    def exceptionHandler(exc):
        raise exc

    def run(self):
        # We expect quite some spinning in below loop, so move any unneeded
        # operation out of it.
        context = self.__context
        poll = self.__poller.poll
        try_lock_events = context.tryLockEvents
        lock_event_waiters = context.lockEventWaiters
        wait_for_event = context.waitForEvent
        unlock_event_waiters = context.unlockEventWaiters
        event_handling_ok = context.eventHandlingOK
        unlock_events = context.unlockEvents
        handle_events_locked = context.handleEventsLocked
        event_handler_active = context.eventHandlerActive
        getNextTimeout = context.getNextTimeout
        exceptionHandler = self.exceptionHandler
        fd_set = self.__fd_set
        while fd_set:
            if try_lock_events():
                lock_event_waiters()
                while event_handler_active():
                    wait_for_event()
                unlock_event_waiters()
            else:
                try:
                    while event_handling_ok():
                        if poll(getNextTimeout()):
                            try:
                                handle_events_locked()
                            except libusb1.USBError:
                                exceptionHandler(sys.exc_info()[1])
                finally:
                    unlock_events()

    def _registerFD(self, fd, events, _):
        self.__poller.register(fd, events)
        self.__fd_set.add(fd)

    def _unregisterFD(self, fd, _):
        self.__fd_set.discard(fd)
        self.__poller.unregister(fd)

class USBPoller(object):
    """
    Class allowing integration of USB event polling in a file-descriptor
    monitoring event loop.

    WARNING: Do not call "poll" from several threads concurently. Do not use
    synchronous USB transfers in a thread while "poll" is running. Doing so
    will result in unnecessarily long pauses in some threads. Opening and/or
    closing devices while polling can cause race conditions to occur.
    """
    def __init__(self, context, poller):
        """
        Create a poller for given context.
        Warning: it will not check if another poller instance was already
        present for that context, and will replace it.

        poller is a polling instance implementing the following methods:
        - register(fd, event_flags)
          event_flags have the same meaning as in poll API (POLLIN & POLLOUT)
        - unregister(fd)
        - poll(timeout)
          timeout being a float in seconds, or negative/None if there is no
          timeout.
          It must return a list of (descriptor, event) pairs.
        Note: USBPoller is itself a valid poller.
        Note2: select.poll uses a timeout in milliseconds, for some reason
        (all other select.* classes use seconds for timeout), so you should
        wrap it to convert & round/truncate timeout.
        """
        self.__context = context
        self.__poller = poller
        self.__fd_set = set()
        context.setPollFDNotifiers(self._registerFD, self._unregisterFD)
        for fd, events in context.getPollFDList():
            self._registerFD(fd, events)

    def __del__(self):
        self.__context.setPollFDNotifiers(None, None)

    def poll(self, timeout=None):
        """
        Poll for events.
        timeout can be a float in seconds, or None for no timeout.
        Returns a list of (descriptor, event) pairs.
        """
        next_usb_timeout = self.__context.getNextTimeout()
        if timeout is None or timeout < 0:
            usb_timeout = next_usb_timeout
        elif next_usb_timeout:
            usb_timeout = min(next_usb_timeout, timeout)
        else:
            usb_timeout = timeout
        event_list = self.__poller.poll(usb_timeout)
        if event_list:
            fd_set = self.__fd_set
            result = [(x, y) for x, y in event_list if x not in fd_set]
            if len(result) != len(event_list):
                self.__context.handleEventsTimeout()
        else:
            result = event_list
            self.__context.handleEventsTimeout()
        return result

    def register(self, fd, events):
        """
        Register an USB-unrelated fd to poller.
        Convenience method.
        """
        if fd in self.__fd_set:
            raise ValueError('This fd is a special USB event fd, it cannot '
                'be polled.')
        self.__poller.register(fd, events)

    def unregister(self, fd):
        """
        Unregister an USB-unrelated fd from poller.
        Convenience method.
        """
        if fd in self.__fd_set:
            raise ValueError('This fd is a special USB event fd, it must '
                'stay registered.')
        self.__poller.unregister(fd)

    def _registerFD(self, fd, events, user_data=None):
        self.register(fd, events)
        self.__fd_set.add(fd)

    def _unregisterFD(self, fd, user_data=None):
        self.__fd_set.discard(fd)
        self.unregister(fd)

class USBDeviceHandle(object):
    """
    Represents an opened USB device.
    """
    __handle = None
    __libusb_close = libusb1.libusb_close
    __USBError = libusb1.USBError
    __LIBUSB_ERROR_NOT_FOUND = libusb1.LIBUSB_ERROR_NOT_FOUND
    __set = set

    def __init__(self, context, handle, device):
        """
        You should not instanciate this class directly.
        Call "open" method on an USBDevice instance to get an USBDeviceHandle
        instance.
        """
        self.__context = context
        # Weak reference to transfers about this device so we can clean up
        # before closing device.
        self.__transfer_set = WeakSet()
        # Strong references to inflight transfers so they do not get freed
        # even if user drops all strong references to them. If this instance
        # is garbage-collected, we close all transfers, so it's fine.
        self.__inflight = inflight = set()
        # XXX: For some reason, doing self.__inflight.{add|remove} inside
        # getTransfer causes extra intermediate python objects for each
        # allocated transfer. Storing them as properties solves this. Found
        # with objgraph.
        self.__inflight_add = inflight.add
        self.__inflight_remove = inflight.remove
        self.__handle = handle
        self.__device = device

    def __del__(self):
        self.close()

    def close(self):
        """
        Close this handle. If not called explicitely, will be called by
        destructor.

        This method cancels any in-flight transfer when it is called. As
        cancellation is not immediate, this method needs to let libusb handle
        events until transfers are actually cancelled.
        In multi-threaded programs, this can lead to stalls. To avoid this,
        do not close nor let GC collect a USBDeviceHandle which has in-flight
        transfers.
        """
        handle = self.__handle
        if handle is not None:
            # Build a strong set from weak self.__transfer_set so we can doom
            # and close all contained transfers.
            # Because of backward compatibility, self.__transfer_set might be a
            # wrapper around WeakKeyDictionary. As it might be modified by gc,
            # we must pop until there is not key left instead of iterating over
            # it.
            weak_transfer_set = self.__transfer_set
            transfer_set = self.__set()
            while True:
                try:
                    transfer = weak_transfer_set.pop()
                except KeyError:
                    break
                transfer_set.add(transfer)
                transfer.doom()
            inflight = self.__inflight
            for transfer in inflight:
                try:
                    transfer.cancel()
                except self.__USBError:
                    if sys.exc_info()[1].value != \
                            self.__LIBUSB_ERROR_NOT_FOUND:
                        raise
            while inflight:
                self.__context.handleEvents()
            for transfer in transfer_set:
                transfer.close()
            self.__libusb_close(handle)
            self.__handle = None

    def getDevice(self):
        """
        Get an USBDevice instance for the device accessed through this handle.
        Useful for example to query its configurations.
        """
        return self.__device

    def getConfiguration(self):
        """
        Get the current configuration number for this device.
        """
        configuration = c_int()
        result = libusb1.libusb_get_configuration(self.__handle,
                                                  byref(configuration))
        if result:
            raise libusb1.USBError(result)
        return configuration.value

    def setConfiguration(self, configuration):
        """
        Set the configuration number for this device.
        """
        result = libusb1.libusb_set_configuration(self.__handle, configuration)
        if result:
            raise libusb1.USBError(result)

    def claimInterface(self, interface):
        """
        Claim (= get exclusive access to) given interface number. Required to
        receive/send data.
        """
        result = libusb1.libusb_claim_interface(self.__handle, interface)
        if result:
            raise libusb1.USBError(result)

    def releaseInterface(self, interface):
        """
        Release interface, allowing another process to use it.
        """
        result = libusb1.libusb_release_interface(self.__handle, interface)
        if result:
            raise libusb1.USBError(result)

    def setInterfaceAltSetting(self, interface, alt_setting):
        """
        Set interface's alternative setting (both parameters are integers).
        """
        result = libusb1.libusb_set_interface_alt_setting(self.__handle,
                                                          interface,
                                                          alt_setting)
        if result:
            raise libusb1.USBError(result)

    def clearHalt(self, endpoint):
        """
        Clear a halt state on given endpoint number.
        """
        result = libusb1.libusb_clear_halt(self.__handle, endpoint)
        if result:
            raise libusb1.USBError(result)

    def resetDevice(self):
        """
        Reinitialise current device.
        Attempts to restore current configuration & alt settings.
        If this fails, will result in a device disconnect & reconnect, so you
        have to close current device and rediscover it (notified by a
        LIBUSB_ERROR_NOT_FOUND error code).
        """
        result = libusb1.libusb_reset_device(self.__handle)
        if result:
            raise libusb1.USBError(result)

    def kernelDriverActive(self, interface):
        """
        Tell whether a kernel driver is active on given interface number.
        """
        result = libusb1.libusb_kernel_driver_active(self.__handle, interface)
        if result == 0:
            is_active = False
        elif result == 1:
            is_active = True
        else:
            raise libusb1.USBError(result)
        return is_active

    def detachKernelDriver(self, interface):
        """
        Ask kernel driver to detach from given interface number.
        """
        result = libusb1.libusb_detach_kernel_driver(self.__handle, interface)
        if result:
            raise libusb1.USBError(result)

    def attachKernelDriver(self, interface):
        """
        Ask kernel driver to re-attach to given interface number.
        """
        result = libusb1.libusb_attach_kernel_driver(self.__handle, interface)
        if result:
            raise libusb1.USBError(result)

    def getSupportedLanguageList(self):
        """
        Return a list of USB language identifiers (as integers) supported by
        current device for its string descriptors.

        Note: language identifiers seem (I didn't check them all...) very
        similar to windows language identifiers, so you may want to use
        locales.windows_locale to get an rfc3066 representation. The 5 standard
        HID language codes are missing though.
        """
        descriptor_string = create_binary_buffer(STRING_LENGTH)
        result = libusb1.libusb_get_string_descriptor(self.__handle,
            0, 0, descriptor_string, sizeof(descriptor_string))
        if result < 0:
            if result == libusb1.LIBUSB_ERROR_PIPE:
                # From libusb_control_transfer doc:
                # control request not supported by the device
                return []
            raise libusb1.USBError(result)
        length = cast(descriptor_string, POINTER(c_ubyte))[0]
        langid_list = cast(descriptor_string, POINTER(c_uint16))
        result = []
        append = result.append
        for offset in xrange(1, length / 2):
            append(libusb1.libusb_le16_to_cpu(langid_list[offset]))
        return result

    def getStringDescriptor(self, descriptor, lang_id):
        """
        Fetch description string for given descriptor and in given language.
        Use getSupportedLanguageList to know which languages are available.
        Return value is an unicode string.
        Return None if there is no such descriptor on device.
        """
        descriptor_string = create_binary_buffer(STRING_LENGTH)
        result = libusb1.libusb_get_string_descriptor(self.__handle,
            descriptor, lang_id, descriptor_string, sizeof(descriptor_string))
        if result == libusb1.LIBUSB_ERROR_NOT_FOUND:
            return None
        if result < 0:
            raise libusb1.USBError(result)
        return descriptor_string.value.decode('UTF-16-LE')

    def getASCIIStringDescriptor(self, descriptor):
        """
        Fetch description string for given descriptor in first available
        language.
        Return value is an ASCII string.
        Return None if there is no such descriptor on device.
        """
        descriptor_string = create_binary_buffer(STRING_LENGTH)
        result = libusb1.libusb_get_string_descriptor_ascii(self.__handle,
             descriptor, descriptor_string, sizeof(descriptor_string))
        if result == libusb1.LIBUSB_ERROR_NOT_FOUND:
            return None
        if result < 0:
            raise libusb1.USBError(result)
        return descriptor_string.value.decode('ASCII')

    # Sync I/O

    def _controlTransfer(self, request_type, request, value, index, data,
                         length, timeout):
        result = libusb1.libusb_control_transfer(self.__handle, request_type,
            request, value, index, data, length, timeout)
        if result < 0:
            raise libusb1.USBError(result)
        return result

    def controlWrite(self, request_type, request, value, index, data,
                     timeout=0):
        """
        Synchronous control write.
        request_type: request type bitmask (bmRequestType), see libusb1
          constants LIBUSB_TYPE_* and LIBUSB_RECIPIENT_*.
        request: request id (some values are standard).
        value, index, data: meaning is request-dependent.
        timeout: in milliseconds, how long to wait for device acknowledgement.
          Set to 0 to disable.

        Returns the number of bytes actually sent.
        """
        request_type = (request_type & ~libusb1.USB_ENDPOINT_DIR_MASK) | \
                        libusb1.LIBUSB_ENDPOINT_OUT
        data = create_binary_buffer(data)
        return self._controlTransfer(request_type, request, value, index, data,
                                     sizeof(data), timeout)

    def controlRead(self, request_type, request, value, index, length,
                    timeout=0):
        """
        Synchronous control read.
        timeout: in milliseconds, how long to wait for data. Set to 0 to
          disable.
        See controlWrite for other parameters description.

        Returns received data.
        """
        request_type = (request_type & ~libusb1.USB_ENDPOINT_DIR_MASK) | \
                        libusb1.LIBUSB_ENDPOINT_IN
        data = create_binary_buffer(length)
        transferred = self._controlTransfer(request_type, request, value,
                                            index, data, length, timeout)
        return data.raw[:transferred]

    def _bulkTransfer(self, endpoint, data, length, timeout):
        transferred = c_int()
        result = libusb1.libusb_bulk_transfer(self.__handle, endpoint,
            data, length, byref(transferred), timeout)
        if result:
            raise libusb1.USBError(result)
        return transferred.value

    def bulkWrite(self, endpoint, data, timeout=0):
        """
        Synchronous bulk write.
        endpoint: endpoint to send data to.
        data: data to send.
        timeout: in milliseconds, how long to wait for device acknowledgement.
          Set to 0 to disable.

        Returns the number of bytes actually sent.
        """
        endpoint = (endpoint & ~libusb1.USB_ENDPOINT_DIR_MASK) | \
                    libusb1.LIBUSB_ENDPOINT_OUT
        data = create_binary_buffer(data)
        return self._bulkTransfer(endpoint, data, sizeof(data), timeout)

    def bulkRead(self, endpoint, length, timeout=0):
        """
        Synchronous bulk read.
        timeout: in milliseconds, how long to wait for data. Set to 0 to
          disable.
        See bulkWrite for other parameters description.

        Returns received data.
        """
        endpoint = (endpoint & ~libusb1.USB_ENDPOINT_DIR_MASK) | \
                    libusb1.LIBUSB_ENDPOINT_IN
        data = create_binary_buffer(length)
        transferred = self._bulkTransfer(endpoint, data, length, timeout)
        return data.raw[:transferred]

    def _interruptTransfer(self, endpoint, data, length, timeout):
        transferred = c_int()
        result = libusb1.libusb_interrupt_transfer(self.__handle, endpoint,
            data, length, byref(transferred), timeout)
        if result:
            raise libusb1.USBError(result)
        return transferred.value

    def interruptWrite(self, endpoint, data, timeout=0):
        """
        Synchronous interrupt write.
        endpoint: endpoint to send data to.
        data: data to send.
        timeout: in milliseconds, how long to wait for device acknowledgement.
          Set to 0 to disable.

        Returns the number of bytes actually sent.
        """
        endpoint = (endpoint & ~libusb1.USB_ENDPOINT_DIR_MASK) | \
                    libusb1.LIBUSB_ENDPOINT_OUT
        data = create_binary_buffer(data)
        return self._interruptTransfer(endpoint, data, sizeof(data), timeout)

    def interruptRead(self, endpoint, length, timeout=0):
        """
        Synchronous interrupt write.
        timeout: in milliseconds, how long to wait for data. Set to 0 to
          disable.
        See interruptRead for other parameters description.

        Returns received data.
        """
        endpoint = (endpoint & ~libusb1.USB_ENDPOINT_DIR_MASK) | \
                    libusb1.LIBUSB_ENDPOINT_IN
        data = create_binary_buffer(length)
        transferred = self._interruptTransfer(endpoint, data, length, timeout)
        return data.raw[:transferred]

    def getTransfer(self, iso_packets=0):
        """
        Get an USBTransfer instance for asynchronous use.
        iso_packets: the number of isochronous transfer descriptors to
          allocate.
        """
        result = USBTransfer(self.__handle, iso_packets,
            self.__inflight_add, self.__inflight_remove,
        )
        self.__transfer_set.add(result)
        return result

class USBConfiguration(object):
    def __init__(self, config):
        """
        You should not instanciate this class directly.
        Call USBDevice methods to get instances of this class.
        """
        if not isinstance(config, libusb1.libusb_config_descriptor):
            raise TypeError('Unexpected descriptor type.')
        self.__config = config

    def getNumInterfaces(self):
        return self.__config.bNumInterfaces

    def getConfigurationValue(self):
        return self.__config.bConfigurationValue

    def getDescriptor(self):
        return self.__config.iConfiguration

    def getAttributes(self):
        return self.__config.bmAttributes

    def getMaxPower(self):
        """
        Returns device's power consumption in mW.
        Beware of unit: USB descriptor uses 2mW increments, this method
        converts it to mW units.
        """
        return self.__config.MaxPower * 2

    def getExtra(self):
        """
        Returns a list of extra (non-basic) descriptors (DFU, HID, ...).
        """
        return libusb1.get_extra(self.__config)

    def __iter__(self):
        """
        Iterates over interfaces available in this configuration, yielding
        USBInterface instances.
        """
        interface_list = self.__config.interface
        for interface_num in xrange(self.getNumInterfaces()):
            yield USBInterface(interface_list[interface_num])

    # BBB
    iterInterfaces = __iter__

    def __getitem__(self, interface):
        """
        Returns an USBInterface instance.
        """
        if not isinstance(interface, int):
            raise TypeError('interface parameter must be an integer')
        if not (0 <= interface < self.getNumInterfaces()):
            raise IndexError('No such interface: %r' % (interface, ))
        return USBInterface(self.__config.interface[interface])

class USBInterface(object):
    def __init__(self, interface):
        """
        You should not instanciate this class directly.
        Call USBConfiguration methods to get instances of this class.
        """
        if not isinstance(interface, libusb1.libusb_interface):
            raise TypeError('Unexpected descriptor type.')
        self.__interface = interface

    def getNumSettings(self):
        return self.__interface.num_altsetting

    def __iter__(self):
        """
        Iterates over settings in this insterface, yielding
        USBInterfaceSetting instances.
        """
        alt_setting_list = self.__interface.altsetting
        for alt_setting_num in xrange(self.getNumSettings()):
            yield USBInterfaceSetting(alt_setting_list[alt_setting_num])

    # BBB
    iterSettings = __iter__

    def __getitem__(self, alt_setting):
        """
        Returns an USBInterfaceSetting instance.
        """
        if not isinstance(alt_setting, int):
            raise TypeError('alt_setting parameter must be an integer')
        if not (0 <= alt_setting < self.getNumSettings()):
            raise IndexError('No such setting: %r' % (alt_setting, ))
        return USBInterfaceSetting(self.__interface.altsetting[alt_setting])

class USBInterfaceSetting(object):
    def __init__(self, alt_setting):
        """
        You should not instanciate this class directly.
        Call USBDevice or USBInterface methods to get instances of this class.
        """
        if not isinstance(alt_setting, libusb1.libusb_interface_descriptor):
            raise TypeError('Unexpected descriptor type.')
        self.__alt_setting = alt_setting

    def getNumber(self):
        return self.__alt_setting.bInterfaceNumber

    def getAlternateSetting(self):
        return self.__alt_setting.bAlternateSetting

    def getNumEndpoints(self):
        return self.__alt_setting.bNumEndpoints

    def getClass(self):
        return self.__alt_setting.bInterfaceClass

    def getSubClass(self):
        return self.__alt_setting.bInterfaceSubClass

    def getClassTupple(self):
        """
        For convenience: class and subclass are probably often matched
        simultaneously.
        """
        alt_setting = self.__alt_setting
        return (alt_setting.bInterfaceClass, alt_setting.bInterfaceSubClass)

    def getProtocol(self):
        return self.__alt_setting.bInterfaceProtocol

    def getDescriptor(self):
        return self.__alt_setting.iInterface

    def getExtra(self):
        return libusb1.get_extra(self.__alt_setting)

    def __iter__(self):
        """
        Iterates over endpoints in this interface setting , yielding
        USBEndpoint instances.
        """
        endpoint_list = self.__alt_setting.endpoint
        for endpoint_num in xrange(self.getNumEndpoints()):
            yield USBEndpoint(endpoint_list[endpoint_num])

    # BBB
    iterEndpoints = __iter__

    def __getitem__(self, endpoint):
        """
        Returns an USBEndpoint instance.
        """
        if not isinstance(endpoint, int):
            raise TypeError('endpoint parameter must be an integer')
        if not (0 <= endpoint < self.getNumEndpoints()):
            raise ValueError('No such endpoint: %r' % (endpoint, ))
        return USBEndpoint(self.__alt_setting.endpoint[endpoint])

class USBEndpoint(object):
    def __init__(self, endpoint):
        if not isinstance(endpoint, libusb1.libusb_endpoint_descriptor):
            raise TypeError('Unexpected descriptor type.')
        self.__endpoint = endpoint

    def getAddress(self):
        return self.__endpoint.bEndpointAddress

    def getAttributes(self):
        return self.__endpoint.bmAttributes

    def getMaxPacketSize(self):
        return self.__endpoint.wMaxPacketSize

    def getInterval(self):
        return self.__endpoint.bInterval

    def getRefresh(self):
        return self.__endpoint.bRefresh

    def getSyncAddress(self):
        return self.__endpoint.bSynchAddress

    def getExtra(self):
        return libusb1.get_extra(self.__endpoint)

class USBDevice(object):
    """
    Represents a USB device.
    """

    __configuration_descriptor_list = ()
    __libusb_unref_device = libusb1.libusb_unref_device
    __libusb_free_config_descriptor = libusb1.libusb_free_config_descriptor
    __byref = byref

    def __init__(self, context, device_p):
        """
        You should not instanciate this class directly.
        Call LibUSBContext methods to receive instances of this class.
        """
        # Important: device_p refcount must be incremented before being given
        # to this constructor. This class will decrement refcount upon
        # destruction.
        self.__context = context
        self.device_p = device_p
        # Fetch device descriptor
        device_descriptor = libusb1.libusb_device_descriptor()
        result = libusb1.libusb_get_device_descriptor(device_p,
            byref(device_descriptor))
        if result:
            raise libusb1.USBError(result)
        self.device_descriptor = device_descriptor
        # Fetch all configuration descriptors
        self.__configuration_descriptor_list = []
        append = self.__configuration_descriptor_list.append
        for configuration_id in xrange(device_descriptor.bNumConfigurations):
            config = libusb1.libusb_config_descriptor_p()
            result = libusb1.libusb_get_config_descriptor(device_p,
                configuration_id, byref(config))
            if result == libusb1.LIBUSB_ERROR_NOT_FOUND:
                # Some devices (ex windows' root hubs) tell they have one
                # configuration, but they have no configuration descriptor.
                continue
            if result:
                raise libusb1.USBError(result)
            append(config.contents)

    def __del__(self):
        self.__libusb_unref_device(self.device_p)
        byref = self.__byref
        for config in self.__configuration_descriptor_list:
            self.__libusb_free_config_descriptor(byref(config))

    def __str__(self):
        return 'Bus %03i Device %03i: ID %04x:%04x' % (
            self.getBusNumber(),
            self.getDeviceAddress(),
            self.getVendorID(),
            self.getProductID(),
        )

    def __getitem__(self, index):
        return USBConfiguration(self.__configuration_descriptor_list[index])

    def iterConfigurations(self):
        for config in self.__configuration_descriptor_list:
            yield USBConfiguration(config)

    # BBB
    iterConfiguations = iterConfigurations

    def iterSettings(self):
        for config in self.__configuration_descriptor_list:
            for interface in USBConfiguration(config):
                for setting in interface:
                    yield setting

    def getBusNumber(self):
        """
        Get device's bus number.
        """
        return libusb1.libusb_get_bus_number(self.device_p)

    def getDeviceAddress(self):
        """
        Get device's address on its bus.
        """
        return libusb1.libusb_get_device_address(self.device_p)

    def getbcdUSB(self):
        """
        Get the USB spec version device complies to, in BCD format.
        """
        return self.device_descriptor.bcdUSB

    def getDeviceClass(self):
        """
        Get device's class id.
        """
        return self.device_descriptor.bDeviceClass

    def getDeviceSubClass(self):
        """
        Get device's subclass id.
        """
        return self.device_descriptor.bDeviceSubClass

    def getDeviceProtocol(self):
        """
        Get device's protocol id.
        """
        return self.device_descriptor.bDeviceProtocol

    def getMaxPacketSize0(self):
        """
        Get device's max packet size for endpoint 0 (control).
        """
        return self.device_descriptor.bMaxPacketSize0

    def getMaxPacketSize(self, endpoint):
        """
        Get device's max packet size for given endpoint.

        Warning: this function will not always give you the expected result.
        See https://libusb.org/ticket/77 . You should instead consult the
        endpoint descriptor of current configuration and alternate setting.
        """
        result = libusb1.libusb_get_max_packet_size(self.device_p, endpoint)
        if result < 0:
            raise libusb1.USBError(result)
        return result

    def getMaxISOPacketSize(self, endpoint):
        """
        Get the maximum size for a single isochronous packet for given
        endpoint.

        Warning: this function will not always give you the expected result.
        See https://libusb.org/ticket/77 . You should instead consult the
        endpoint descriptor of current configuration and alternate setting.
        """
        result = libusb1.libusb_get_max_iso_packet_size(self.device_p, endpoint)
        if result < 0:
            raise libusb1.USBError(result)
        return result

    def getVendorID(self):
        """
        Get device's vendor id.
        """
        return self.device_descriptor.idVendor

    def getProductID(self):
        """
        Get device's product id.
        """
        return self.device_descriptor.idProduct

    def getbcdDevice(self):
        """
        Get device's release number.
        """
        return self.device_descriptor.bcdDevice

    def getSupportedLanguageList(self):
        """
        Get the list of language ids device has string descriptors for.
        """
        temp_handle = self.open()
        return temp_handle.getSupportedLanguageList()

    def _getStringDescriptor(self, descriptor, lang_id):
        if descriptor == 0:
            result = None
        else:
            temp_handle = self.open()
            result = temp_handle.getStringDescriptor(descriptor, lang_id)
        return result

    def _getASCIIStringDescriptor(self, descriptor):
        if descriptor == 0:
            result = None
        else:
            temp_handle = self.open()
            result = temp_handle.getASCIIStringDescriptor(descriptor)
        return result

    def getManufacturer(self):
        """
        Get device's manufaturer name.
        Note: opens the device temporarily.
        """
        return self._getASCIIStringDescriptor(
            self.device_descriptor.iManufacturer)

    def getProduct(self):
        """
        Get device's product name.
        Note: opens the device temporarily.
        """
        return self._getASCIIStringDescriptor(self.device_descriptor.iProduct)

    def getSerialNumber(self):
        """
        Get device's serial number.
        Note: opens the device temporarily.
        """
        return self._getASCIIStringDescriptor(
            self.device_descriptor.iSerialNumber)

    def getNumConfigurations(self):
        """
        Get device's number of possible configurations.
        """
        return self.device_descriptor.bNumConfigurations

    def getDeviceSpeed(self):
        """
        Get device's speed (see libusb1.libusb_speed for possible return
        values).
        """
        return libusb1.libusb_get_device_speed(self.device_p)

    def open(self):
        """
        Open device.
        Returns an USBDeviceHandle instance.
        """
        handle = libusb1.libusb_device_handle_p()
        result = libusb1.libusb_open(self.device_p, byref(handle))
        if result:
            raise libusb1.USBError(result)
        return USBDeviceHandle(self.__context, handle, self)

_zero_tv = libusb1.timeval(0, 0)
_zero_tv_p = byref(_zero_tv)

class USBContext(object):
    """
    libusb1 USB context.

    Provides methods to enumerate & look up USB devices.
    Also provides access to global (device-independent) libusb1 functions.
    """
    __libusb_exit = libusb1.libusb_exit
    __context_p = None
    __added_cb = None
    __removed_cb = None
    __libusb_set_pollfd_notifiers = libusb1.libusb_set_pollfd_notifiers

    def _validContext(func):
        # Defined inside LibUSBContext so we can access "self.__*".
        def wrapper(self, *args, **kw):
            self.__context_cond.acquire()
            self.__context_refcount += 1
            self.__context_cond.release()
            try:
                if self.__context_p is not None:
                    return func(self, *args, **kw)
            finally:
                self.__context_cond.acquire()
                self.__context_refcount -= 1
                if not self.__context_refcount:
                    self.__context_cond.notifyAll()
                self.__context_cond.release()
        return wrapper

    def __init__(self):
        """
        Create a new USB context.
        """
        # Used to prevent an exit to cause a segfault if a concurrent thread
        # is still in libusb.
        self.__context_refcount = 0
        self.__context_cond = threading.Condition()
        context_p = libusb1.libusb_context_p()
        result = libusb1.libusb_init(byref(context_p))
        if result:
            raise libusb1.USBError(result)
        self.__context_p = context_p

    def __del__(self):
        # Avoid locking.
        # XXX: Assumes __del__ should not normally be called while any
        # instance's method is being executed. It seems unlikely (they hold a
        # reference to their instance).
        self._exit()

    def exit(self):
        """
        Close (destroy) this USB context.

        When this method has been called, methods on its instance will
        become mosty no-ops, returning None.
        """
        self.__context_cond.acquire()
        try:
            while self.__context_refcount and self.__context_p is not None:
                self.__context_cond.wait()
            self._exit()
        finally:
            self.__context_cond.notifyAll()
            self.__context_cond.release()

    def _exit(self):
        context_p = self.__context_p
        if context_p is not None:
            self.__libusb_exit(context_p)
            self.__context_p = None
            self.__added_cb = None
            self.__removed_cb = None

    @_validContext
    def getDeviceList(self, skip_on_access_error=False, skip_on_error=False):
        """
        Return a list of all USB devices currently plugged in, as USBDevice
        instances.

        skip_on_error (bool)
            If True, ignore devices which raise USBError.

        skip_on_access_error (bool)
            DEPRECATED. Alias for skip_on_error.
        """
        skip_on_error = skip_on_error or skip_on_access_error
        device_p_p = libusb1.libusb_device_p_p()
        libusb_device_p = libusb1.libusb_device_p
        device_list_len = libusb1.libusb_get_device_list(self.__context_p,
                                                         byref(device_p_p))
        if device_list_len < 0:
            raise libusb1.USBError(device_list_len)
        try:
            result = []
            append = result.append
            for device_p in device_p_p[:device_list_len]:
                try:
                    # Instanciate our own libusb_device_p object so we can free
                    # libusb-provided device list. Is this a bug in ctypes that it
                    # doesn't copy pointer value (=pointed memory address) ? At
                    # least, it's not so convenient and forces using such weird
                    # code.
                    device = USBDevice(self, libusb_device_p(device_p.contents))
                except libusb1.USBError:
                    if not skip_on_error:
                        raise
                else:
                    append(device)
        finally:
            libusb1.libusb_free_device_list(device_p_p, 0)
        return result

    def getByVendorIDAndProductID(self, vendor_id, product_id,
            skip_on_access_error=False, skip_on_error=False):
        """
        Get the first USB device matching given vendor and product ids.
        Returns an USBDevice instance, or None if no present device match.
        skip_on_error (bool)
            (see getDeviceList)
        skip_on_access_error (bool)
            (see getDeviceList)
        """
        for device in self.getDeviceList(
                skip_on_access_error=skip_on_access_error,
                skip_on_error=skip_on_error):
            if device.getVendorID() == vendor_id and \
                    device.getProductID() == product_id:
                result = device
                break
        else:
            result = None
        return result

    def openByVendorIDAndProductID(self, vendor_id, product_id,
            skip_on_access_error=False, skip_on_error=False):
        """
        Get the first USB device matching given vendor and product ids.
        Returns an USBDeviceHandle instance, or None if no present device
        match.
        skip_on_error (bool)
            (see getDeviceList)
        skip_on_access_error (bool)
            (see getDeviceList)
        """
        result = self.getByVendorIDAndProductID(vendor_id, product_id,
            skip_on_access_error=skip_on_access_error,
            skip_on_error=skip_on_error)
        if result is not None:
            result = result.open()
        return result

    @_validContext
    def getPollFDList(self):
        """
        Return file descriptors to be used to poll USB events.
        You should not have to call this method, unless you are integrating
        this class with a polling mechanism.
        """
        pollfd_p_p = libusb1.libusb_get_pollfds(self.__context_p)
        if not pollfd_p_p:
            errno = get_errno()
            if errno:
                raise OSError(errno)
            else:
                # Assume not implemented
                raise NotImplementedError("Your libusb doesn't seem to "
                    "implement pollable FDs")
        try:
            result = []
            append = result.append
            fd_index = 0
            while pollfd_p_p[fd_index]:
                append((pollfd_p_p[fd_index].contents.fd,
                        pollfd_p_p[fd_index].contents.events))
                fd_index += 1
        finally:
            _free(pollfd_p_p)
        return result

    @_validContext
    def handleEvents(self):
        """
        Handle any pending event (blocking).
        See libusb1 documentation for details (there is a timeout, so it's
        not "really" blocking).
        """
        result = libusb1.libusb_handle_events(self.__context_p)
        if result:
            raise libusb1.USBError(result)

    # TODO: handleEventsCompleted

    @_validContext
    def handleEventsTimeout(self, tv=0):
        """
        Handle any pending event.
        If tv is 0, will return immediately after handling already-pending
        events.
        Otherwise, defines the maximum amount of time to wait for events, in
        seconds.
        """
        if tv is None:
            tv = 0
        tv_s = int(tv)
        tv = libusb1.timeval(tv_s, int((tv - tv_s) * 1000000))
        result = libusb1.libusb_handle_events_timeout(self.__context_p,
            byref(tv))
        if result:
            raise libusb1.USBError(result)

    # TODO: handleEventsTimeoutCompleted

    @_validContext
    def setPollFDNotifiers(self, added_cb=None, removed_cb=None,
            user_data=None):
        """
        Give libusb1 methods to call when it should add/remove file descriptor
        for polling.
        You should not have to call this method, unless you are integrating
        this class with a polling mechanism.
        """
        if added_cb is None:
            added_cb = POINTER(None)
        else:
            added_cb = libusb1.libusb_pollfd_added_cb_p(added_cb)
        if removed_cb is None:
            removed_cb = POINTER(None)
        else:
            removed_cb = libusb1.libusb_pollfd_removed_cb_p(removed_cb)
        self.__added_cb = added_cb
        self.__removed_cb = removed_cb
        self.__libusb_set_pollfd_notifiers(self.__context_p, added_cb,
                                            removed_cb, user_data)

    @_validContext
    def getNextTimeout(self):
        """
        Returns the next internal timeout that libusb needs to handle, in
        seconds, or None if no timeout is needed.
        You should not have to call this method, unless you are integrating
        this class with a polling mechanism.
        """
        timeval = libusb1.timeval()
        result = libusb1.libusb_get_next_timeout(self.__context_p,
            byref(timeval))
        if result == 0:
            result = None
        elif result == 1:
            result = timeval.tv_sec + (timeval.tv_usec * 0.000001)
        else:
            raise libusb1.USBError(result)
        return result

    @_validContext
    def setDebug(self, level):
        """
        Set debugging level.
        Note: depending on libusb compilation settings, this might have no
        effect.
        """
        libusb1.libusb_set_debug(self.__context_p, level)

    @_validContext
    def tryLockEvents(self):
        """
        See libusb_try_lock_events doc.
        """
        return libusb1.libusb_try_lock_events(self.__context_p)

    @_validContext
    def lockEvents(self):
        """
        See libusb_lock_events doc.
        """
        libusb1.libusb_lock_events(self.__context_p)

    @_validContext
    def lockEventWaiters(self):
        """
        See libusb_lock_event_waiters doc.
        """
        libusb1.libusb_lock_event_waiters(self.__context_p)

    @_validContext
    def waitForEvent(self):
        """
        See libusb_wait_for_event doc.
        """
        libusb1.libusb_wait_for_event(self.__context_p)

    @_validContext
    def unlockEventWaiters(self):
        """
        See libusb_unlock_event_waiters doc.
        """
        libusb1.libusb_unlock_event_waiters(self.__context_p)

    @_validContext
    def eventHandlingOK(self):
        """
        See libusb_event_handling_ok doc.
        """
        return libusb1.libusb_event_handling_ok(self.__context_p)

    @_validContext
    def unlockEvents(self):
        """
        See libusb_unlock_events doc.
        """
        libusb1.libusb_unlock_events(self.__context_p)

    @_validContext
    def handleEventsLocked(self):
        """
        See libusb_handle_events_locked doc.
        """
        # XXX: does tv parameter need to be exposed ?
        result = libusb1.libusb_handle_events_locked(self.__context_p,
            _zero_tv_p)
        if result:
            raise libusb1.USBError(result)

    @_validContext
    def eventHandlerActive(self):
        """
        See libusb_event_handler_active doc.
        """
        return libusb1.libusb_event_handler_active(self.__context_p)

    def hasCapability(self, capability):
        """
        Tests feature presence.
        See libusb1.libusb_capability .
        """
        return libusb1.libusb_has_capability(capability)

del USBContext._validContext

def getVersion():
    """
    Returns underlying libusb's version information as a 6-namedtuple (or
    6-tuple if namedtuples are not avaiable):
    - major
    - minor
    - micro
    - nano
    - rc
    - describe
    Returns (0, 0, 0, 0, '', '') if libusb doesn't have required entry point.
    """
    version = libusb1.libusb_get_version().contents
    return Version(version.major, version.minor, version.micro, version.nano,
        version.rc, version.describe)

class LibUSBContext(USBContext):
    """
    Backward-compatibility alias for USBContext.
    """
    def __init__(self):
        warnings.warn('LibUSBContext is being renamed to USBContext',
            DeprecationWarning)
        super(LibUSBContext, self).__init__()

