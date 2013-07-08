# -*- coding: utf-8 -*

import re
import time
import usb1


PART_BLOCKSIZE = 0x400 # must be multiple of 512
PART_OFF_INCR  = PART_BLOCKSIZE >> 9
RKFT_BLOCKSIZE = 0x4000 # must be multiple of 512
RKFT_OFF_INCR  = RKFT_BLOCKSIZE >> 9
RKFT_DISPLAY   = 0x1000

RK_VENDER_ID   = 0x2207
RK_PRODUCT_IDS = set([
  0x290a,
  0x281a,
  0x300a, # RK3066
  0x310b, # RK3188
])

PARTITION_PATTERN = re.compile(r'0x([0-9a-fA-F]*?)@(0x[0-9a-fA-F]*?)\((.*?)\)')

RKFT_CID     = 4
RKFT_FLAG    = 12
RKFT_COMMAND = 13
RKFT_OFFSET  = 17
RKFT_SIZE    = 23
USB_CMD = [chr(0)] * 31
USB_CMD[0:4] = 'USBC'
global_cmd_id = -1


def next_cmd_id():
  global global_cmd_id
  global_cmd_id = (global_cmd_id + 1) & 0xFF
  return chr(global_cmd_id)


def prepare_cmd(flag, command, offset, size):
  USB_CMD[RKFT_CID ] = next_cmd_id();
  USB_CMD[RKFT_FLAG] = chr(flag);
  USB_CMD[RKFT_SIZE] = chr(size);
  USB_CMD[RKFT_COMMAND    ] = chr((command >> 24) & 0xFF)
  USB_CMD[RKFT_COMMAND + 1] = chr((command >> 16) & 0xFF)
  USB_CMD[RKFT_COMMAND + 2] = chr((command >>  8) & 0xFF)
  USB_CMD[RKFT_COMMAND + 3] = chr((command      ) & 0xFF)
  USB_CMD[RKFT_OFFSET     ] = chr((offset  >> 24) & 0xFF)
  USB_CMD[RKFT_OFFSET  + 1] = chr((offset  >> 16) & 0xFF)
  USB_CMD[RKFT_OFFSET  + 2] = chr((offset  >>  8) & 0xFF)
  USB_CMD[RKFT_OFFSET  + 3] = chr((offset       ) & 0xFF)
  return USB_CMD


def is_rk_device(device):
  return (device.getVendorID() == RK_VENDER_ID and
          device.getProductID() in RK_PRODUCT_IDS);


def list_devices():
  device_uids = set([])
  device_list = []

  context = None
  try:
    context = usb1.USBContext()
    context.setDebug(3)
    devices = context.getDeviceList()
    for device in devices:
      if is_rk_device(device):
        dev_uid = '%d:%d' % (device.getBusNumber(), device.getDeviceAddress())
        device_uids.add(dev_uid)
        device_list.append(
            (device.getBusNumber(),
             device.getDeviceAddress(),
             device.getVendorID(),
             device.getProductID()))
  finally:
    if context:
      del context

  return (device_uids, device_list)


class RkOperation(object):
  def __init__(self, logger, bus_id, dev_id):
    self.__logger = logger
    self.__context = usb1.USBContext()
    self.__context.setDebug(3)

    devices = self.__context.getDeviceList()
    for device in devices:
      if (is_rk_device(device)
          and device.getBusNumber() == bus_id and
          device.getDeviceAddress() == dev_id):
        self.__dev_handle = device.open()

    if not self.__dev_handle:
      raise Exception('Failed to open device.')


  def __del__(self):
    if self.__dev_handle:
      self.__dev_handle.releaseInterface(0)
      del self.__dev_handle
    if self.__context:
      del self.__context


  def __init_device(self):
    if self.__dev_handle.kernelDriverActive(0):
      print self.__dev_handle.detachKernelDriver(0)
    self.__dev_handle.claimInterface(0)

    # Init
    print self.__dev_handle.bulkWrite(
        2, ''.join(prepare_cmd(0x80, 0x00060000, 0x00000000, 0x00000000)))
    self.__dev_handle.bulkRead(1, 13)

    # sleep for 20ms
    time.sleep(0.02)


  def load_partitions(self):
    partitions = []

    self.__init_device()

    self.__logger.print_dividor()
    self.__logger.log('\tLoading partition information\n')
    self.__dev_handle.bulkWrite(
        2, ''.join(prepare_cmd(0x80, 0x000a1400, 0x00000000, PART_OFF_INCR)))

    content = self.__dev_handle.bulkRead(1, PART_BLOCKSIZE)
    self.__dev_handle.bulkRead(1, 13)
    for line in content.split('\n'):
      self.__logger.log('\t%s' % line)
      if line.startswith('CMDLINE:'):
        # return a list of tuple (size, unused, offset, part_name)
        return re.findall(PARTITION_PATTERN, line)

    self.__logger.log('\tDone!\n')
    return partitions


  def flash_image_file(self, offset, size, file_name):
    self.__init_device()

    self.__logger.print_dividor()
    self.__logger.log('\tWriting file %s to part 0x%08X@0x%08X\n' % (
        file_name, offset, size))
    with open(file_name) as fh:
      while size > 0:
        if offset % RKFT_DISPLAY == 0:
          self.__logger.log(
              '\twriting flash memory at offset 0x%08x\n' % offset)

        buf = bytearray(RKFT_BLOCKSIZE)
        block = fh.read(RKFT_BLOCKSIZE)
        buf[:len(block)] = block
        self.__dev_handle.bulkWrite(
            2, ''.join(prepare_cmd(0x80, 0x000a1500, offset, RKFT_OFF_INCR)))
        self.__dev_handle.bulkWrite(2, str(buf))
        self.__dev_handle.bulkRead(1, 13)

        offset += RKFT_OFF_INCR
        size   -= RKFT_OFF_INCR

    self.__logger.log('\tDone!\n')


  def erase_partition(self, offset, size):
    self.__init_device()

    self.__logger.print_dividor()
    self.__logger.log('\tErasing partition 0x%08X@0x%08X\n' % (offset, size))
    buf = ''.join([chr(0xFF)] * RKFT_BLOCKSIZE)
    while size > 0:
      if offset % RKFT_DISPLAY == 0:
        self.__logger.log(
            '\terasing flash memory at offset 0x%08x\n' % offset)

      self.__dev_handle.bulkWrite(
          2, ''.join(prepare_cmd(0x80, 0x000a1500, offset, RKFT_OFF_INCR)))
      self.__dev_handle.bulkWrite(2, buf)
      self.__dev_handle.bulkRead(1, 13)

      offset += RKFT_OFF_INCR
      size   -= RKFT_OFF_INCR

    self.__logger.log('\tDone!\n')


  def reboot(self):
    self.__init_device()
    self.__dev_handle.bulkWrite(
        2, ''.join(prepare_cmd(0x00, 0x0006ff00, 0x00000000, 0x00)))
    self.__dev_handle.bulkRead(1, 13)
    self.__logger.print_dividor()
    self.__logger.log('\tRebooting device\n')
    self.__logger.log('\tDone!\n')

