#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Termux USB transport using termux-usb API + libusb via ctypes
# (c) B.Kerler 2018-2025 under GPLv3 license

import array
import fcntl
import json
import logging
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
from binascii import hexlify

import ctypes
from ctypes import (
    c_int, c_uint8, c_uint16, c_uint32, c_uint64,
    c_void_p, c_char_p, c_size_t, POINTER, byref, Structure, cast
)

try:
    from edlclient.Library.utils import *
    from edlclient.Library.Connection.devicehandler import DeviceClass
except ImportError:
    from Library.utils import *
    from Library.Connection.devicehandler import DeviceClass

USB_DIR_OUT = 0
USB_DIR_IN = 0x80

MAX_USB_BULK_BUFFER_SIZE = 16384

TERMUX_USB_CMD = "termux-usb"


def _is_termux():
    return os.environ.get("TERMUX_VERSION") is not None or "com.termux" in __file__


class LibUsbCtx:
    _lib = None
    _ctx = None
    _instance = None

    @classmethod
    def get_lib(cls):
        if cls._lib is None:
            lib_paths = [
                "libusb-1.0.so",
                "libusb-1.0.so.0",
                "/data/data/com.termux/files/usr/lib/libusb-1.0.so",
                "/system/lib64/libusb-1.0.so",
                "/system/lib/libusb-1.0.so",
            ]
            for path in lib_paths:
                try:
                    cls._lib = ctypes.CDLL(path)
                    break
                except OSError:
                    continue
            if cls._lib is None:
                raise RuntimeError(
                    "libusb-1.0 not found. Install it: pkg install libusb"
                )
        return cls._lib

    @classmethod
    def get_ctx(cls):
        if cls._ctx is None:
            lib = cls.get_lib()
            lib.libusb_set_option.argtypes = [c_void_p, c_int]
            lib.libusb_set_option.restype = c_int
            lib.libusb_set_option(None, 2)
            ctx = c_void_p()
            ret = lib.libusb_init(byref(ctx))
            if ret != 0:
                raise RuntimeError(f"libusb_init failed: {ret}")
            cls._ctx = ctx
        return cls._ctx

    @classmethod
    def cleanup(cls):
        if cls._ctx is not None:
            lib = cls.get_lib()
            lib.libusb_exit(cls._ctx)
            cls._ctx = None


def _setup_libusb_functions(lib):
    lib.libusb_init.argtypes = [POINTER(c_void_p)]
    lib.libusb_init.restype = c_int

    lib.libusb_exit.argtypes = [c_void_p]
    lib.libusb_exit.restype = None

    lib.libusb_wrap_sys_device.argtypes = [c_void_p, c_int, POINTER(c_void_p)]
    lib.libusb_wrap_sys_device.restype = c_int

    lib.libusb_get_device_list.argtypes = [c_void_p, POINTER(POINTER(c_void_p))]
    lib.libusb_get_device_list.restype = c_int

    lib.libusb_free_device_list.argtypes = [POINTER(c_void_p), c_int]
    lib.libusb_free_device_list.restype = None

    lib.libusb_get_device.argtypes = [c_void_p]
    lib.libusb_get_device.restype = c_void_p

    lib.libusb_get_device_descriptor.argtypes = [c_void_p, c_void_p]
    lib.libusb_get_device_descriptor.restype = c_int

    lib.libusb_get_active_config_descriptor.argtypes = [c_void_p, POINTER(c_void_p)]
    lib.libusb_get_active_config_descriptor.restype = c_int

    lib.libusb_free_config_descriptor.argtypes = [c_void_p]
    lib.libusb_free_config_descriptor.restype = None

    lib.libusb_open.argtypes = [c_void_p, POINTER(c_void_p)]
    lib.libusb_open.restype = c_int

    lib.libusb_close.argtypes = [c_void_p]
    lib.libusb_close.restype = None

    lib.libusb_claim_interface.argtypes = [c_void_p, c_int]
    lib.libusb_claim_interface.restype = c_int

    lib.libusb_release_interface.argtypes = [c_void_p, c_int]
    lib.libusb_release_interface.restype = c_int

    lib.libusb_detach_kernel_driver.argtypes = [c_void_p, c_int]
    lib.libusb_detach_kernel_driver.restype = c_int

    lib.libusb_set_configuration.argtypes = [c_void_p, c_int]
    lib.libusb_set_configuration.restype = c_int

    lib.libusb_get_configuration.argtypes = [c_void_p, POINTER(c_int)]
    lib.libusb_get_configuration.restype = c_int

    lib.libusb_bulk_transfer.argtypes = [
        c_void_p, c_uint8, POINTER(c_uint8), c_int, POINTER(c_int), c_uint32
    ]
    lib.libusb_bulk_transfer.restype = c_int

    lib.libusb_control_transfer.argtypes = [
        c_void_p, c_uint8, c_uint8, c_uint16, c_uint16,
        POINTER(c_uint8), c_uint16, c_uint32
    ]
    lib.libusb_control_transfer.restype = c_int

    lib.libusb_get_string_descriptor_ascii.argtypes = [
        c_void_p, c_uint8, c_char_p, c_int
    ]
    lib.libusb_get_string_descriptor_ascii.restype = c_int

    lib.libusb_kernel_driver_active.argtypes = [c_void_p, c_int]
    lib.libusb_kernel_driver_active.restype = c_int

    lib.libusb_attach_kernel_driver.argtypes = [c_void_p, c_int]
    lib.libusb_attach_kernel_driver.restype = c_int

    lib.libusb_reset_device.argtypes = [c_void_p]
    lib.libusb_reset_device.restype = c_int


class DeviceDescriptor(Structure):
    _fields_ = [
        ("bLength", c_uint8),
        ("bDescriptorType", c_uint8),
        ("bcdUSB", c_uint16),
        ("bDeviceClass", c_uint8),
        ("bDeviceSubClass", c_uint8),
        ("bDeviceProtocol", c_uint8),
        ("bMaxPacketSize0", c_uint8),
        ("idVendor", c_uint16),
        ("idProduct", c_uint16),
        ("bcdDevice", c_uint16),
        ("iManufacturer", c_uint8),
        ("iProduct", c_uint8),
        ("iSerialNumber", c_uint8),
        ("bNumConfigurations", c_uint8),
    ]


class InterfaceDescriptor(Structure):
    _fields_ = [
        ("bLength", c_uint8),
        ("bDescriptorType", c_uint8),
        ("bInterfaceNumber", c_uint8),
        ("bAlternateSetting", c_uint8),
        ("bNumEndpoints", c_uint8),
        ("bInterfaceClass", c_uint8),
        ("bInterfaceSubClass", c_uint8),
        ("bInterfaceProtocol", c_uint8),
        ("iInterface", c_uint8),
    ]


class EndpointDescriptor(Structure):
    _fields_ = [
        ("bLength", c_uint8),
        ("bDescriptorType", c_uint8),
        ("bEndpointAddress", c_uint8),
        ("bmAttributes", c_uint8),
        ("wMaxPacketSize", c_uint16),
        ("bInterval", c_uint8),
    ]


class ConfigDescriptor(Structure):
    _fields_ = [
        ("bLength", c_uint8),
        ("bDescriptorType", c_uint8),
        ("wTotalLength", c_uint16),
        ("bNumInterfaces", c_uint8),
        ("bConfigurationValue", c_uint8),
        ("iConfiguration", c_uint8),
        ("bmAttributes", c_uint8),
        ("MaxPower", c_uint8),
    ]


_libusb_functions_setup = False


def _ensure_libusb():
    global _libusb_functions_setup
    lib = LibUsbCtx.get_lib()
    if not _libusb_functions_setup:
        _setup_libusb_functions(lib)
        _libusb_functions_setup = True
    return lib


class termux_usb_class(DeviceClass):
    def __init__(self, loglevel=logging.INFO, portconfig=None, devclass=-1, serial_number=None):
        super().__init__(loglevel, portconfig, devclass)
        self.serial_number = serial_number
        self.is_serial = False
        self.buffer = array.array('B', [0]) * 1048576
        self.lib = None
        self.dev_handle = None
        self.EP_IN = None
        self.EP_OUT = None
        self.device_node = None
        self.timeout = 5000
        self.interface_number = 0
        try:
            self.lib = _ensure_libusb()
        except RuntimeError as e:
            self.error(f"libusb not available: {e}")
            self.lib = None

    @staticmethod
    def _create_fd_helper_script():
        script = r'''#!/data/data/com.termux/files/usr/bin/python3
import os, socket, struct
s=int(os.environ["EDL_SOCK_FD"])
u=int(os.environ["TERMUX_USB_FD"])
sock=socket.fromfd(s,socket.AF_UNIX,socket.SOCK_STREAM)
sock.sendmsg([b"1"],[(socket.SOL_SOCKET,socket.SCM_RIGHTS,struct.pack("i",u))])
sock.close()
'''
        f = tempfile.NamedTemporaryFile(
            mode='w', suffix='.py', delete=False,
            prefix='edl_fd_helper_', dir=os.environ.get('TMPDIR', '/tmp')
        )
        f.write(script)
        f.close()
        os.chmod(f.name, 0o755)
        return f.name

    def _termux_usb_list(self):
        try:
            result = subprocess.run(
                [TERMUX_USB_CMD, "-l"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                self.debug(f"termux-usb -l failed: {result.stderr}")
                return []
            devices = json.loads(result.stdout)
            return devices
        except (json.JSONDecodeError, subprocess.TimeoutExpired, FileNotFoundError) as e:
            self.debug(f"termux-usb list error: {e}")
            return []

    def _termux_usb_open(self, device_node, timeout=60):
        parent_sock, child_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        for fd in (child_sock.fileno(),):
            flags = fcntl.fcntl(fd, fcntl.F_GETFD)
            fcntl.fcntl(fd, fcntl.F_SETFD, flags & ~fcntl.FD_CLOEXEC)
        helper_path = self._create_fd_helper_script()
        env = os.environ.copy()
        env['EDL_SOCK_FD'] = str(child_sock.fileno())
        try:
            proc = subprocess.Popen(
                [TERMUX_USB_CMD, '-e', helper_path, '-E', '-r', device_node],
                pass_fds=[child_sock.fileno()],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except Exception as e:
            self.error(f"Failed to start termux-usb: {e}")
            parent_sock.close(); child_sock.close()
            try: os.unlink(helper_path)
            except: pass
            return None
        child_sock.close()
        parent_sock.settimeout(timeout)
        try:
            msg, ancdata, msg_flags, from_addr = parent_sock.recvmsg(
                1024, socket.CMSG_SPACE(struct.calcsize('i'))
            )
            for cmsg_level, cmsg_type, cmsg_data in ancdata:
                if cmsg_level == socket.SOL_SOCKET and cmsg_type == socket.SCM_RIGHTS:
                    fds = array.array('i')
                    fds.frombytes(cmsg_data[:struct.calcsize('i')])
                    if len(fds) > 0:
                        parent_sock.close()
                        try: os.unlink(helper_path)
                        except: pass
                        return fds[0]
        except socket.timeout:
            self.error("Timeout waiting for USB fd from helper (grant permission on device?)")
        except Exception as e:
            self.debug(f"Error receiving fd: {e}")
        self.error(f"termux-usb open failed for {device_node}")
        parent_sock.close()
        try: os.unlink(helper_path)
        except: pass
        return None

    def _read_device_descriptor(self, dev_handle):
        desc = DeviceDescriptor()
        ret = self.lib.libusb_get_device_descriptor(dev_handle, byref(desc))
        if ret != 0:
            return None
        return desc

    def detectdevices(self):
        if not _is_termux():
            self.debug("Not running in Termux environment")
            return []
        devices = self._termux_usb_list()
        if not devices:
            self.debug("No USB devices found via termux-usb")
            return []
        self.debug(f"USB devices available: {devices}")
        return devices

    def _try_device(self, device_node, EP_IN, EP_OUT):
        fd = self._termux_usb_open(device_node)
        if fd is None:
            return None

        ctx = LibUsbCtx.get_ctx()
        dev_handle = c_void_p()

        try:
            ret = self.lib.libusb_wrap_sys_device(ctx, fd, byref(dev_handle))
            if ret != 0:
                self.debug(f"libusb_wrap_sys_device({device_node}) failed: {ret}")
                os.close(fd)
                return None
        except Exception as e:
            self.debug(f"libusb_wrap_sys_device error: {e}")
            os.close(fd)
            return None

        try:
            device_ptr = self.lib.libusb_get_device(dev_handle)
            if not device_ptr:
                self.debug("libusb_get_device returned NULL")
                self.lib.libusb_close(dev_handle)
                os.close(fd)
                return None
            desc = DeviceDescriptor()
            ret = self.lib.libusb_get_device_descriptor(device_ptr, byref(desc))
            if ret != 0:
                self.lib.libusb_close(dev_handle)
                os.close(fd)
                return None
            vid = desc.idVendor
            pid = desc.idProduct
        except Exception as e:
            self.debug(f"read descriptor error: {e}")
            self.lib.libusb_close(dev_handle)
            os.close(fd)
            return None

        for usbid in self.portconfig:
            if vid == usbid[0] and pid == usbid[1]:
                self.info(f"Detected EDL device {hex(vid)}:{hex(pid)} at {device_node}")
                self.vid = vid
                self.pid = pid
                self.dev_handle = dev_handle
                self.fd = fd
                self.device_node = device_node
                return dev_handle

        self.lib.libusb_close(dev_handle)
        os.close(fd)
        return None

    def connect(self, EP_IN=-1, EP_OUT=-1, portname: str = ""):
        if self.connected:
            self.close()
            self.connected = False

        if self.lib is None:
            self.error("libusb not loaded. Run: pkg install libusb")
            return False

        if portname:
            candidate_nodes = [portname]
        else:
            candidate_nodes = self.detectdevices()
            if not candidate_nodes:
                self.debug("No USB devices detected via termux-usb")
                return False

        dev_handle = None
        for node in candidate_nodes:
            dev_handle = self._try_device(node, EP_IN, EP_OUT)
            if dev_handle is not None:
                break

        if dev_handle is None:
            self.debug("No EDL device found among available USB devices")
            return False

        self.dev_handle = dev_handle
        self.connected = True

        try:
            self.configuration = self._get_active_config_descriptor()
        except Exception:
            self.configuration = None

        try:
            self._claim_interface(self.interface_number)
        except Exception as e:
            self.debug(f"claim interface: {e}")

        if EP_IN == -1 or EP_OUT == -1:
            self._find_endpoints()
        else:
            self.EP_IN_addr = EP_IN
            self.EP_OUT_addr = EP_OUT

        if self.EP_IN_addr is not None and self.EP_OUT_addr is not None:
            self.connected = True
            return True

        self.warning("Could not find endpoints, using defaults")
        self.EP_IN_addr = 0x81
        self.EP_OUT_addr = 0x01
        return True

    def _get_active_config_descriptor(self):
        class ConfigDescRaw(Structure):
            _fields_ = [
                ("bLength", c_uint8),
                ("bDescriptorType", c_uint8),
                ("wTotalLength", c_uint16),
                ("bNumInterfaces", c_uint8),
                ("bConfigurationValue", c_uint8),
                ("iConfiguration", c_uint8),
                ("bmAttributes", c_uint8),
                ("MaxPower", c_uint8),
            ]
        cfg_ptr = c_void_p()
        ret = self.lib.libusb_get_active_config_descriptor(
            self.dev_handle, byref(cfg_ptr)
        )
        if ret != 0 or not cfg_ptr:
            return None
        cfg = cast(cfg_ptr, POINTER(ConfigDescRaw))[0]
        self.lib.libusb_free_config_descriptor(cfg_ptr)
        return cfg

    def _find_endpoints(self):
        self.EP_IN_addr = None
        self.EP_OUT_addr = None
        self.maxsize = 512

        class UsbEndpointDesc(Structure):
            _fields_ = [
                ("bLength", c_uint8),
                ("bDescriptorType", c_uint8),
                ("bEndpointAddress", c_uint8),
                ("bmAttributes", c_uint8),
                ("wMaxPacketSize", c_uint16),
                ("bInterval", c_uint8),
            ]

        class UsbInterfaceDesc(Structure):
            _fields_ = [
                ("bLength", c_uint8),
                ("bDescriptorType", c_uint8),
                ("bInterfaceNumber", c_uint8),
                ("bAlternateSetting", c_uint8),
                ("bNumEndpoints", c_uint8),
                ("bInterfaceClass", c_uint8),
                ("bInterfaceSubClass", c_uint8),
                ("bInterfaceProtocol", c_uint8),
                ("iInterface", c_uint8),
            ]

        cfg = self._get_active_config_descriptor()
        if cfg is None:
            return

        num_ifaces = cfg.bNumInterfaces
        buf = (c_uint8 * 1024)()
        ret = self.lib.libusb_control_transfer(
            self.dev_handle,
            0x80 | 0x02, 0x06,
            (3 << 8), 0,
            buf, 1024, 1000
        )
        if ret > 0:
            pos = 0
            while pos < ret:
                desc_len = buf[pos]
                desc_type = buf[pos + 1]
                if desc_len == 0:
                    break
                if desc_type == 4:
                    ep = UsbEndpointDesc()
                    ctypes.memmove(byref(ep), ctypes.addressof(buf) + pos, desc_len)
                    addr = ep.bEndpointAddress
                    if addr & USB_DIR_IN:
                        if self.EP_IN_addr is None:
                            self.EP_IN_addr = addr
                            self.maxsize = ep.wMaxPacketSize
                    else:
                        if self.EP_OUT_addr is None:
                            self.EP_OUT_addr = addr
                pos += desc_len

    def _claim_interface(self, iface_num):
        try:
            ret = self.lib.libusb_kernel_driver_active(self.dev_handle, iface_num)
            if ret == 1:
                self.debug("Detaching kernel driver")
                self.lib.libusb_detach_kernel_driver(self.dev_handle, iface_num)
        except Exception:
            pass

        ret = self.lib.libusb_claim_interface(self.dev_handle, iface_num)
        if ret != 0:
            self.debug(f"claim interface {iface_num}: {ret}")

    def setLineCoding(self, baudrate=None, parity=0, databits=8, stopbits=1):
        sbits = {1: 0, 1.5: 1, 2: 2}
        if stopbits is not None:
            self.stopbits = stopbits if stopbits in sbits else 0
        if databits is not None:
            self.databits = databits
        if parity is not None:
            self.parity = parity
        if baudrate is not None:
            self.baudrate = baudrate

        linecode = [
            self.baudrate & 0xff,
            (self.baudrate >> 8) & 0xff,
            (self.baudrate >> 16) & 0xff,
            (self.baudrate >> 24) & 0xff,
            sbits.get(self.stopbits, 0),
            self.parity,
            self.databits
        ]
        req_type = (0 << 7) | (1 << 5) | 1
        data = (c_uint8 * len(linecode))(*linecode)
        try:
            self.lib.libusb_control_transfer(
                self.dev_handle, req_type, 0x20,
                0, 1, data, len(linecode), 1000
            )
        except Exception as e:
            self.debug(f"setLineCoding: {e}")

    def setbreak(self):
        req_type = (0 << 7) | (1 << 5) | 1
        try:
            self.lib.libusb_control_transfer(
                self.dev_handle, req_type, 0x23, 0, 1, None, 0, 1000
            )
        except Exception as e:
            self.debug(f"setbreak: {e}")

    def setcontrollinestate(self, RTS=None, DTR=None, isFTDI=False):
        ctrlstate = (2 if RTS else 0) + (1 if DTR else 0)
        req_type = (0 << 7) | (2 if isFTDI else 1 << 5) | (0 if isFTDI else 1)
        brequest = 1 if isFTDI else 0x22
        try:
            self.lib.libusb_control_transfer(
                self.dev_handle, req_type, brequest,
                ctrlstate, 1, None, 0, 1000
            )
        except Exception as e:
            self.debug(f"setcontrollinestate: {e}")

    def flush(self):
        pass

    def close(self, reset=False):
        if self.connected:
            try:
                if self.dev_handle:
                    if reset:
                        self.lib.libusb_reset_device(self.dev_handle)
                    try:
                        self.lib.libusb_release_interface(
                            self.dev_handle, self.interface_number
                        )
                    except Exception:
                        pass
                    self.lib.libusb_close(self.dev_handle)
            except Exception as err:
                self.debug(f"close error: {err}")
            try:
                if hasattr(self, 'fd') and self.fd is not None:
                    os.close(self.fd)
            except Exception:
                pass
            self.dev_handle = None
            self.fd = None
            self.connected = False

    def write(self, command, pktsize=None):
        if pktsize is None:
            pktsize = MAX_USB_BULK_BUFFER_SIZE

        if isinstance(command, str):
            command = bytes(command, 'utf-8')

        if self.EP_OUT_addr is None:
            self.error("EP_OUT not configured")
            return False

        if command == b'':
            try:
                transferred = c_int()
                self.lib.libusb_bulk_transfer(
                    self.dev_handle, self.EP_OUT_addr, None, 0,
                    byref(transferred), 1000
                )
            except Exception:
                pass
            return True

        pos = 0
        i = 0
        while pos < len(command):
            try:
                chunk = command[pos:pos + pktsize]
                chunk_len = len(chunk)
                buf = (c_uint8 * chunk_len).from_buffer_copy(chunk)
                transferred = c_int()
                ret = self.lib.libusb_bulk_transfer(
                    self.dev_handle, self.EP_OUT_addr, buf, chunk_len,
                    byref(transferred), 5000
                )
                if ret != 0:
                    self.debug(f"bulk write error: {ret}")
                    i += 1
                    if i >= 3:
                        return False
                    continue
                pos += pktsize
            except Exception as err:
                self.debug(f"write exception: {err}")
                i += 1
                if i >= 3:
                    return False

        self.verify_data(bytearray(command), "TX:")
        return True

    def usbread(self, resplen=None, timeout=0):
        if timeout == 0:
            timeout = 1000
        if timeout is None:
            timeout = 100
        if resplen is None:
            resplen = self.maxsize
        if resplen <= 0:
            return b""

        if self.EP_IN_addr is None:
            self.error("EP_IN not configured")
            return b""

        res = bytearray()
        loglevel = self.loglevel

        while len(res) < resplen:
            try:
                bufsize = min(resplen, 16384)
                buf = (c_uint8 * bufsize)()
                transferred = c_int()
                ret = self.lib.libusb_bulk_transfer(
                    self.dev_handle, self.EP_IN_addr, buf, bufsize,
                    byref(transferred), timeout
                )
                if ret == 0:
                    res.extend(buf[:transferred.value])
                    if transferred.value < bufsize:
                        break
                elif ret == -7:
                    break
                elif ret == -8:
                    self.error("USB Overflow")
                    return b""
                else:
                    self.debug(f"bulk read ret={ret}")
                    return b""
            except Exception as e:
                self.debug(f"usbread exception: {e}")
                return b""

        if loglevel == logging.DEBUG:
            self.verify_data(res, "RX:")

        return bytes(res)

    def ctrl_transfer(self, bmRequestType, bRequest, wValue, wIndex, data_or_wLength):
        if isinstance(data_or_wLength, int):
            buf = (c_uint8 * data_or_wLength)()
            ret = self.lib.libusb_control_transfer(
                self.dev_handle, bmRequestType, bRequest,
                wValue, wIndex, buf, data_or_wLength, 1000
            )
            if ret > 0:
                return buf[0] | (buf[1] << 8)
            return 0
        else:
            data = bytes(data_or_wLength)
            buf = (c_uint8 * len(data)).from_buffer_copy(data)
            ret = self.lib.libusb_control_transfer(
                self.dev_handle, bmRequestType, bRequest,
                wValue, wIndex, buf, len(data), 1000
            )
            return ret

    def usbwrite(self, data, pktsize=None):
        if pktsize is None:
            pktsize = len(data)
        return self.write(data, pktsize)

    def usbreadwrite(self, data, resplen):
        self.usbwrite(data)
        return self.usbread(resplen)

    def getInterfaceCount(self):
        cfg = self._get_active_config_descriptor()
        if cfg is not None:
            return cfg.bNumInterfaces
        return 0

    class deviceclass:
        vid = 0
        pid = 0

        def __init__(self, vid, pid):
            self.vid = vid
            self.pid = pid

    def detectdevices_libusb(self):
        ctx = LibUsbCtx.get_ctx()
        dev_list = POINTER(c_void_p)()
        count = self.lib.libusb_get_device_list(ctx, byref(dev_list))
        if count < 0:
            return []

        ids = []
        desc = DeviceDescriptor()
        for i in range(count):
            dev = dev_list[i]
            if not dev:
                break
            ret = self.lib.libusb_get_device_descriptor(dev, byref(desc))
            if ret == 0:
                ids.append(self.deviceclass(desc.idVendor, desc.idProduct))
        self.lib.libusb_free_device_list(dev_list, 1)
        return ids
