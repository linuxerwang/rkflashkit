#! /usr/bin/env python
# -*- coding: utf-8 -*

from datetime import datetime
import time
import os
import rktalk

BLACK, RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, WHITE = range(8)

def format(fg=None, bg=None, bright=False, bold=False, dim=False, reset=False):
    # manually derived from http://en.wikipedia.org/wiki/ANSI_escape_code#Codes
    codes = []
    if reset: codes.append("0")
    else:
        if not fg is None: codes.append("3%d" % (fg))
        if not bg is None:
            if not bright: codes.append("4%d" % (bg))
            else: codes.append("10%d" % (bg))
        if bold: codes.append("1")
        elif dim: codes.append("2")
        else: codes.append("22")
    return "\033[%sm" % (";".join(codes))

class ConsoleLogger(object):
  def __init__(self, use_color=False):
    self.WARN_COLOR = self.SUCC_COLOR = self.RESET_COLOR = ""
    if use_color:
        self.WARN_COLOR = format(fg=RED)
        self.SUCC_COLOR = format(fg=GREEN)
        self.RESET_COLOR = format(reset=True)

  def log(self, message):
      print message

  def print_dividor(self):
    current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    self.log('\n%s============= %s ============%s\n\n' % (
      self.WARN_COLOR, current_time, self.RESET_COLOR))

  def print_done(self):
    self.log('\t%sDone!%s\n' % (self.SUCC_COLOR, self.RESET_COLOR))


  def print_error(self, message):
    self.log('%sERROR:%s %s' % (self.WARN_COLOR, self.RESET_COLOR, message))

def get_devices():
  device_store = []
  device_uids, device_list = rktalk.list_devices()
  for bus_id, dev_id, vendor_id, prod_id in device_list:
    dev_name = '0x%04x:0x%04x' % (vendor_id, prod_id)
    device_store.append(
          (dev_name, (bus_id, dev_id, vendor_id, prod_id)))
  return device_store

def wait_for_one_device():
  while True:
    devices = get_devices()
    if not devices:
      print "No devices found, retry..."
      time.sleep(1)
    else:
      print "Found devices:"
      for dev in devices:
        print dev[0]
      if len(devices) > 1:
        fatal("More than one device found.")
      break
  return devices[0]

class Operation(object):
  def __init__(self, logger, bus_id, dev_id):
    self.op = rktalk.RkOperation(logger, bus_id, dev_id)

  def __enter__(self):
    return self.op

  def __exit__(self, *args):
    self.op = None #del self.op

class CliMain(object):
  def __init__(self):
    self.logger = ConsoleLogger(use_color=True)
    self.bus_id = 0
    self.dev_id = 0
    self.partition = {}

  def main(self, args):
    if args[0] in ("help", "-h", "--help"):
      self.usage()
      return 0
    dev = wait_for_one_device()
    self.bus_id = dev[1][0]
    self.dev_id = dev[1][1]
    self.load_partitions()
    self.parse_and_execute(args)
    return 0

  def parse_and_execute(self, args):
    while args:
      if args[0] == "part":
        # part
        self.load_partitions()
        args = args[1:]
      elif args[0] == "flash":
        # flash [@boot boot.img ...]
        args = args[1:]
        while len(args) >= 2 \
              and args[0][0] == "@":
          self.flash_image(args[0], args[1])
          args = args[2:]
      elif args[0] == "cmp":
        # cmp @boot boot.img
        self.compare_imagefile(args[1], args[2])
        args = args[3:]
      elif args[0] == "backup":
        # backup @boot new_boot.img
        self.backup_partition(args[1], args[2])
        args = args[3:]
      elif args[0] == "erase":
        # backup @boot new_boot.img
        self.erase_partition(args[1])
        args = args[2:]
      elif args[0] == "reboot":
        # reboot
        self.reboot()
        break
      else:
        self.usage()
        raise RuntimeError("Unknown command: %s", args[0])

  def usage(self):
    print """Usage: <cmd> [args] [<cmd> [args]...]

part                              List partition
flash @<PARTITION> <IMAGE FILE>   Flash partition with image file
cmp @<PARTITION> <IMAGE FILE>     Compare partition with image file
backup @<PARTITION> <IMAGE FILE>  Backup partition to image file
erase  @<PARTITION>               Erase partition
reboot                            Reboot device

For example, flash device with boot.img and kernel.img, then reboot:

  sudo rkflashkit flash @boot boot.img @kernel.img kernel.img reboot"""
  def get_operation(self):
    assert self.bus_id and self.dev_id
    return Operation(self.logger, self.bus_id, self.dev_id)

  def load_partitions(self):
    partitions = {}
    with self.get_operation() as op:
      loaded_parts = op.load_partitions()
    self.log('Partitions:')
    for size, offset, name in loaded_parts:
      size = int(size, 16)
      offset = int(offset, 16)
      partitions[name] = (offset, size)
      self.log('%-12s (0x%08x @ 0x%08x) %4d MiB' % (name, size, offset, size * 512 / 1024 / 1024))
    self.partitions = partitions

  def get_partition(self, part_name):
    if part_name[0] == '@':
      part_name = part_name[1:]
    return self.partitions[part_name] # (offset, size)

  def flash_image(self, part_name, image_file):
    with self.get_operation() as op:
      if part_name == '@parameter':
        op.flash_parameter(image_file)
      else:
        offset, size = self.get_partition(part_name)
        op.flash_image_file(offset, size, image_file)

  def compare_imagefile(self, part_name, image_file):
    offset, size = self.get_partition(part_name)
    with self.get_operation() as op:
      op.cmp_part_with_file(offset, size, image_file)

  def backup_partition(self, part_name, image_file):
    with self.get_operation() as op:
      if part_name == '@parameter':
        op.backup_parameter(image_file)
      else:
        offset, size = self.get_partition(part_name)
        op.backup_partition(offset, size, image_file)

  def erase_partition(self, part_name):
    offset, size = self.get_partition(part_name)
    with self.get_operation() as op:
      op.erase_partition(offset, size)

  def reboot(self):
    with self.get_operation() as op:
      op.reboot()

  def log(self, message):
    self.logger.log(message)
