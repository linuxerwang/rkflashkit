# -*- coding: utf-8 -*

import re
import time
import io
import usb1
from . import rkcrc


PART_BLOCKSIZE = 0x800 # must be multiple of 512
PART_OFF_INCR  = PART_BLOCKSIZE >> 9
RKFT_BLOCKSIZE = 0x4000 # must be multiple of 512
RKFT_OFF_INCR  = RKFT_BLOCKSIZE >> 9
RKFT_DISPLAY   = 0x1000

RK_VENDOR_ID   = 0x2207
RK_PRODUCT_IDS = set([
  0x290a, # RK2906
  0x292a, # RK2928
  0x292c, # RK3026/RK3028
  0x281a,
  0x300a, # RK3066
  0x0010, # RK3168 ???
  0x300b, # RK3168 ???
  0x310b, # RK3188
  0x310c, # RK3128
  0x320a, # RK3288
  0x320b, # RK3229
  0x330c, # RK3399
])

#(read endpoint, write endpoint)
RK_DEVICE_ENDPOINTS = {
  0x290a: (0x01, 0x02), # RK2906
  0x292a: (0x01, 0x02), # RK2928
  0x292c: (0x01, 0x02), # RK3026/RK3028
  0x281a: (0x01, 0x02),
  0x300a: (0x01, 0x02), # RK3066
  0x0010: (0x01, 0x02), # RK3168 ???
  0x300b: (0x01, 0x02), # RK3168 ???
  0x310b: (0x01, 0x02), # RK3188
  0x310c: (0x01, 0x02), # RK3128
  0x320a: (0x01, 0x02), # RK3288
  0x320b: (0x01, 0x02), # RK3229
  0x330c: (0x81, 0x01), # RK3399
}

PARTITION_PATTERN = re.compile(r'(-|0x[0-9a-fA-F]+)@(0x[0-9a-fA-F]+)\((.*?)\)')

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
  return (device.getVendorID() == RK_VENDOR_ID and
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

        product_id = device.getProductID()
        self.__read_endpoint = RK_DEVICE_ENDPOINTS[product_id][0]
        self.__write_endpoint = RK_DEVICE_ENDPOINTS[product_id][1]
        self.__dev_handle = device.open()

    if not self.__dev_handle:
      raise Exception('Failed to open device.')


  def __del__(self):
    try:
      if self.__dev_handle:
        self.__dev_handle.releaseInterface(0)
        del self.__dev_handle
    except Exception as e:
      pass
    if self.__context:
      del self.__context


  def __init_device(self):
    if self.__dev_handle.kernelDriverActive(0):
      self.__dev_handle.detachKernelDriver(0)
    self.__dev_handle.claimInterface(0)

    # Init
    self.__dev_handle.bulkWrite(self.__write_endpoint,
        ''.join(prepare_cmd(0x80, 0x00060000, 0x00000000, 0x00000000)))
    self.__dev_handle.bulkRead(self.__read_endpoint, 13)

    # sleep for 20ms
    time.sleep(0.02)


  def __cmp_part_with_file(self, offset, size, file_obj):
    if True:
      while size > 0:
        fh = file_obj
        if offset % RKFT_DISPLAY == 0:
          self.__logger.log(
              '\treading flash memory at offset 0x%08X\n' % offset)

        block1 = fh.read(RKFT_BLOCKSIZE)
        self.__dev_handle.bulkWrite(self.__write_endpoint,
            ''.join(prepare_cmd(0x80, 0x000a1400, offset, RKFT_OFF_INCR)))
        block2 = self.__dev_handle.bulkRead(self.__read_endpoint,
            RKFT_BLOCKSIZE)
        self.__dev_handle.bulkRead(self.__read_endpoint, 13)

        if len(block1) == len(block2):
          if block1 != block2:
            self.__logger.print_error(
                '\tFlash memory at 0x%08X is differnt from file!\n' % offset)
        else:
          if len(block1) == 0:
            break
          block2 = block2[:len(block1)]
          if block1 != block2:
            self.__logger.print_error(
                '\tFlash memory at 0x%08X is differnt from file!\n' % offset)

        offset += RKFT_OFF_INCR
        size   -= RKFT_OFF_INCR

  def load_partitions(self):
    partitions = []

    self.__init_device()

    self.__logger.print_dividor()
    self.__logger.log('\tReading flash information\n')
    self.__dev_handle.bulkWrite(self.__write_endpoint,
        ''.join(prepare_cmd(0x80, 0x00061a00, 0x00000000, 0x00000000)))
    content = self.__dev_handle.bulkRead(self.__read_endpoint, 512)
    self.__dev_handle.bulkRead(self.__read_endpoint, 13)
    flash_size = (ord(content[0])) | (ord(content[1]) << 8) | (ord(content[2]) << 16) | (ord(content[3]) << 24)

    self.__logger.log('\tLoading partition information\n')
    self.__dev_handle.bulkWrite(self.__write_endpoint,
        ''.join(prepare_cmd(0x80, 0x000a1400, 0x00000000, PART_OFF_INCR)))
    content = self.__dev_handle.bulkRead(self.__read_endpoint, PART_BLOCKSIZE)
    self.__dev_handle.bulkRead(self.__read_endpoint, 13)

    for line in content.split('\n'):
      self.__logger.log('\t%s' % line)
      if line.startswith('CMDLINE:'):
        # return a list of tuple (size, unused, offset, part_name)
        self.__logger.log('\n\n\tPartitions:\n')
        for size, offset, name in re.findall(PARTITION_PATTERN, line):
          offset = int(offset, 16)
          if size == '-':
            size = flash_size - offset
          else:
            size = int(size, 16)
          self.__logger.log('\t%-12s (0x%08X @ 0x%08X) %4d MiB\n' % (name, size, offset, size * 512 / 1024 / 1024))
          partitions.append((size, offset, name))
        break
    self.__logger.print_done()
    return partitions

  def read_flashinfo(self):
    self.__init_device()

    self.__logger.print_dividor()
    self.__logger.log('\tReading flash information\n')
    self.__dev_handle.bulkWrite(self.__write_endpoint,
        ''.join(prepare_cmd(0x80, 0x00061a00, 0x00000000, 0x00000000)))
    content = self.__dev_handle.bulkRead(self.__read_endpoint, 512)
    self.__dev_handle.bulkRead(self.__read_endpoint, 13)
    # uint32_t flash_size;
    # uint16_t block_size;
    # uint8_t page_size;
    # uint8_t ecc_bits;
    # uint8_t access_time;
    # uint8_t manufacturer_id;
    # uint8_t chip_select;
    # only return flash_size here
    flash_size = (ord(content[0])) | (ord(content[1]) << 8) | (ord(content[2]) << 16) | (ord(content[3]) << 24)
    self.__logger.log('Flash size: %.2f GiB' % (flash_size * 512.0 / 1024 / 1024 / 1024))
    self.__logger.print_done()
    return (flash_size, )

  def flash_parameter(self, parameter_file):
    with open(parameter_file) as fh:
      data = fh.read()
      buf = rkcrc.make_parameter_image(data)
    assert len(buf) <= PART_BLOCKSIZE
    with io.BytesIO(buf) as fh:
      self.__logger.print_dividor()
      self.__logger.log('\tWriting parameter file %s\n' % (parameter_file))
      self.__flash_image_file(0x00000000, PART_BLOCKSIZE, fh)

  def backup_parameter(self, parameter_file):
    self.__logger.print_dividor()
    self.__logger.log('\tBackuping parameter to file %s\n' % (parameter_file))
    with io.BytesIO() as fh:
      self.__dump_partition(0x00000000, PART_BLOCKSIZE, fh)
      data = fh.getvalue()
    data = rkcrc.verify_parameter_image(data)
    if data:
      with open(parameter_file, 'wb') as f:
        f.write(data)
    else:
      self.__logger.print_error(
        '\tInvalid parameter file!\n')

  def __flash_image_file(self, offset, size, file_obj):
    self.__init_device()
    if True:
      fh = file_obj
      while size > 0:
        block = fh.read(RKFT_BLOCKSIZE)
        if not block:
          break
        buf = bytearray(RKFT_BLOCKSIZE)
        buf[:len(block)] = block

        if offset % RKFT_DISPLAY == 0:
          self.__logger.log(
              '\twriting flash memory at offset 0x%08X\n' % offset)

        self.__dev_handle.bulkWrite(self.__write_endpoint,
            ''.join(prepare_cmd(0x80, 0x000a1500, offset, RKFT_OFF_INCR)))
        self.__dev_handle.bulkWrite(self.__write_endpoint, str(buf))
        self.__dev_handle.bulkRead(self.__read_endpoint, 13)

        offset += RKFT_OFF_INCR
        size   -= RKFT_OFF_INCR


  def flash_image_file(self, offset, size, file_name):
    self.__init_device()

    original_offset, original_size = offset, size

    self.__logger.print_dividor()
    self.__logger.log('\tWriting file %s to partition 0x%08X@0x%08X\n\n' % (
        file_name, size, offset))
    with open(file_name) as fh:
      self.__flash_image_file(offset, size, fh)
    self.__logger.print_done()

    # Validate partition.
    self.__logger.log('\n')
    self.__logger.log('\tComparing partition 0x%08X@0x%08X with file %s\n\n' % (
        size, offset, file_name))
    with open(file_name) as fh:
      self.__cmp_part_with_file(original_offset, original_size, fh)
    self.__logger.print_done()

  def cmp_part_with_file(self, offset, size, file_name):
    self.__init_device()
    self.__logger.print_dividor()
    self.__logger.log('\tComparing partition 0x%08X@0x%08X with file %s\n\n' % (
        offset, size, file_name))
    with open(file_name) as fh:
      self.__cmp_part_with_file(offset, size, fh)
    self.__logger.print_done()


  def __dump_partition(self, offset, size, file_obj):
    if True:
      fh = file_obj
      while size > 0:
        if offset % RKFT_DISPLAY == 0:
          self.__logger.log(
              '\treading flash memory at offset 0x%08X\n' % offset)

        self.__dev_handle.bulkWrite(self.__write_endpoint,
            ''.join(prepare_cmd(0x80, 0x000a1400, offset, RKFT_OFF_INCR)))
        block = self.__dev_handle.bulkRead(self.__read_endpoint, RKFT_BLOCKSIZE)
        self.__dev_handle.bulkRead(self.__read_endpoint, 13)
        if size < RKFT_BLOCKSIZE and len(block) < size:
          block = block[:size]
        if block:
          fh.write(block)

        offset += RKFT_OFF_INCR
        size   -= RKFT_OFF_INCR

  def backup_partition(self, offset, size, file_name):
    self.__init_device()

    self.__logger.print_dividor()
    self.__logger.log('\tBackup partition 0x%08X@0x%08X to file %s\n\n' % (
        size, offset, file_name))
    with open(file_name, 'w') as fh:
      self.__dump_partition(offset, size, fh)
    self.__logger.print_done()

    # Verify backup.
    self.__logger.log('\n')
    with open(file_name) as fh:
      self.__cmp_part_with_file(offset, size, fh)


  def erase_partition(self, offset, size):
    self.__init_device()

    self.__logger.print_dividor()
    self.__logger.log('\tErasing partition 0x%08X@0x%08X\n\n' % (size, offset))
    buf = ''.join([chr(0xFF)] * RKFT_BLOCKSIZE)
    while size > 0:
      if offset % RKFT_DISPLAY == 0:
        self.__logger.log(
            '\terasing flash memory at offset 0x%08X\n' % offset)

      self.__dev_handle.bulkWrite(self.__write_endpoint,
          ''.join(prepare_cmd(0x80, 0x000a1500, offset, RKFT_OFF_INCR)))
      self.__dev_handle.bulkWrite(self.__write_endpoint, buf)
      self.__dev_handle.bulkRead(self.__read_endpoint, 13)

      offset += RKFT_OFF_INCR
      size   -= RKFT_OFF_INCR

    self.__logger.print_done()


  def reboot(self):
    self.__init_device()
    self.__dev_handle.bulkWrite(self.__write_endpoint,
        ''.join(prepare_cmd(0x00, 0x0006ff00, 0x00000000, 0x00)))
    self.__dev_handle.bulkRead(self.__read_endpoint, 13)
    self.__logger.print_dividor()
    self.__logger.log('\tRebooting device\n')
    self.__logger.print_done()
