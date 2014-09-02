#! /usr/bin/env python
# -*- coding: utf-8 -*

from datetime import datetime
import time
import os
import rktalk

class ConsoleLogger(object):
  def __init__(self):
    pass

  def log(self, message, tag=None):
    if tag:
      print "[%s] %s" % (tag, message)
    else:
      print message

  def print_dividor(self):
    current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    self.log('\n============= %s ============\n\n' % current_time)

  def print_done(self):
    self.log('\tDone!\n')


  def print_error(self, message):
    self.log(message, "ERROR")

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
      time.sleep(2)
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
    self.logger = ConsoleLogger()
    self.bus_id = 0
    self.dev_id = 0
    self.partition = {}

  def main(self, args):
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
        raise RuntimeError("Unknown command: %s", args[0])

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
    offset, size = self.get_partition(part_name)
    with self.get_operation() as op:
      op.flash_image_file(offset, size, image_file)

  def compare_imagefile(self, part_name, image_file):
    offset, size = self.get_partition(part_name)
    with self.get_operation() as op:
      op.cmp_part_with_file(offset, size, image_file)

  def backup_partition(self, part_name, image_file):
    offset, size = self.get_partition(part_name)
    with self.get_operation() as op:
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
