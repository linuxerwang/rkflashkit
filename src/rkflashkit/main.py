#! /usr/bin/env python
# -*- coding: utf-8 -*

from datetime import datetime

import pygtk
pygtk.require('2.0')

import glib
import gtk
import os
import rktalk


DEFAULT_WINDOW_WIDTH  = 1000
DEFAULT_WINDOW_HEIGHT = 650
MESSAGE_FLASH = (
    'Are you sure to flash image file %s to partition "%s"?')
MESSAGE_ERASE = 'Are you sure to erase partition 0x%08X@0x%08X?'
MESSAGE_REBOOT = 'Are you sure to reboot the device?'


def wrap_with_scorlled_window(widget):
  scroll = gtk.ScrolledWindow()
  scroll.add(widget)
  scroll.set_policy(gtk.POLICY_AUTOMATIC, gtk.POLICY_AUTOMATIC)
  scroll.set_shadow_type(gtk.SHADOW_ETCHED_IN)
  scroll.set_border_width(1)
  return scroll


def confirm(parent, message):
  dialog = gtk.Dialog(message, parent, gtk.DIALOG_MODAL, (
      gtk.STOCK_YES, gtk.RESPONSE_YES,
      gtk.STOCK_CANCEL, gtk.RESPONSE_CANCEL,
  ))
  dialog.vbox.pack_start(gtk.Label(message))
  dialog.vbox.show_all()

  try:
    response = dialog.run()
    if response == gtk.RESPONSE_YES:
      return True
  finally:
    dialog.destroy()

  return False


class BoxFrame(gtk.Frame):
  def __init__(self, caption, opt_hbox=True, spacing=0):
    gtk.Frame.__init__(self, caption)
    if opt_hbox:
      self.__box = gtk.HBox(spacing=spacing)
    else:
      self.__box = gtk.VBox(spacing=spacing)
    self.add(self.__box)
    self.__box.set_border_width(10)


  def pack_start(self, widget, **kwargs):
    self.__box.pack_start(widget, **kwargs)


class Logger(object):
  def __init__(self, text_view):
    self.__text_view = text_view
    self.__text_buffer = text_view.get_buffer()
    self.__end_mark = self.__text_buffer.create_mark(
        None, self.__text_buffer.get_end_iter(), True)
    self.__first_dividor = True


  def log(self, message):
    self.__text_buffer.insert(self.__text_buffer.get_end_iter(), message)

    # Scroll to the end of the text view
    self.__text_buffer.move_mark(
        self.__end_mark, self.__text_buffer.get_end_iter())
    self.__text_view.scroll_to_mark(self.__end_mark, 0.0)

    # Make the UI responsive
    while gtk.events_pending(): gtk.main_iteration()


  def print_dividor(self):
    current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    self.log('\n============= %s ============\n\n' % current_time)


class MainWindow(gtk.Window):
  def __init__(self):
    gtk.Window.__init__(self)

    self.__device_uids = set([])

    self.set_title('RkFlashKit')
    self.set_icon(self.render_icon('rkflashkit', gtk.ICON_SIZE_MENU))
    self.connect('delete_event', self.__on_delete_window)
    self.set_default_size(DEFAULT_WINDOW_WIDTH, 650)

    hpaned = gtk.HPaned()
    hpaned.set_position(400)
    self.add(hpaned)
    self.__create_ui(hpaned)

    self.__timer_id = glib.timeout_add_seconds(3, self.__check_devices)

    # Check devices
    self.__check_devices()

    self.show_all()


  def __create_ui(self, paned):
    vbox = gtk.VBox(spacing=10)
    vbox.set_border_width(10)
    paned.add1(vbox)
    self.__create_left_ui(vbox)

    vbox = gtk.VBox()
    vbox.set_border_width(10)
    paned.add2(vbox)
    self.__create_right_ui(vbox)


  def __create_left_ui(self, box):
    # Device selector

    frame = BoxFrame('Devices')
    box.pack_start(frame, expand=False)

    self.__device_liststore = gtk.ListStore(str, object)
    self.__device_selector = gtk.ComboBox(self.__device_liststore)
    cell = gtk.CellRendererText()
    self.__device_selector.pack_start(cell, True)
    self.__device_selector.add_attribute(cell, 'text', 0)
    self.__device_selector.connect('changed', self.__on_device_changed)
    frame.pack_start(self.__device_selector)

    # NAND partition selector

    frame = BoxFrame('NAND Partitions')
    box.pack_start(frame, expand=False)

    self.__partition_liststore = gtk.ListStore(str, int, int)
    self.__partition_selector = gtk.ComboBox(self.__partition_liststore)
    cell = gtk.CellRendererText()
    self.__partition_selector.pack_start(cell, True)
    self.__partition_selector.add_attribute(cell, 'text', 0)
    self.__partition_selector.connect('changed', self.__refresh_buttons)
    frame.pack_start(self.__partition_selector)

    # Image file selector

    frame = BoxFrame('Image File to Flash')
    box.pack_start(frame, expand=False)

    self.__image_entry = gtk.Entry()
    self.__image_entry.connect('changed', self.__refresh_buttons)
    frame.pack_start(self.__image_entry)

    self.__image_select_button = gtk.Button('Choose')
    self.__image_select_button.connect('clicked', self.__choose_image_file)
    frame.pack_start(self.__image_select_button, expand=False, fill=False)

    # Action buttons

    frame = BoxFrame('Actions', opt_hbox=False, spacing=20)
    box.pack_start(frame, expand=False)

    self.__flash_button = gtk.Button('Flash image')
    self.__flash_button.connect('clicked', self.__flash_image_file)
    frame.pack_start(self.__flash_button, expand=False, fill=False)

    self.__erase_button = gtk.Button('Erase Partition')
    self.__erase_button.connect('clicked', self.__erase_partition)
    frame.pack_start(self.__erase_button, expand=False, fill=False)

    self.__reboot_button = gtk.Button('Reboot Device')
    self.__reboot_button.connect('clicked', self.__reboot_device)
    frame.pack_start(self.__reboot_button, expand=False, fill=False)

    self.__clear_log_button = gtk.Button('Clear Log')
    self.__clear_log_button.connect('clicked', self.__clear_log)
    frame.pack_start(self.__clear_log_button, expand=False, fill=False)


  def __create_right_ui(self, box):
    self.__log_text_view = gtk.TextView()
    self.__log_text_view.set_editable(False)
    self.__log_text_view.set_wrap_mode(gtk.WRAP_CHAR)
    self.__logger = Logger(self.__log_text_view)
    box.pack_start(wrap_with_scorlled_window(self.__log_text_view))


  def __on_delete_window(self, window, event):
    gtk.main_quit()
    return True


  def __clear_log(self, widget):
    self.__log_text_view.get_buffer().set_text('')


  def __choose_image_file(self, widget):
    file_chooser = gtk.FileChooserDialog(
        'Choose Image File to Flash',
        self,
        gtk.FILE_CHOOSER_ACTION_OPEN, (
            gtk.STOCK_CANCEL, gtk.RESPONSE_CANCEL,
            gtk.STOCK_OK, gtk.RESPONSE_OK)
    )

    current_file = self.__image_entry.get_text().strip()
    if current_file:
      file_chooser.set_current_folder(os.path.dirname(current_file))

    try:
      response = file_chooser.run()
      if response == gtk.RESPONSE_OK:
        self.__image_entry.set_text(file_chooser.get_filename())
    finally:
      file_chooser.destroy()


  def __flash_image_file(self, widget):
    image_file = self.__image_entry.get_text().strip()
    device_info, = self.__device_liststore.get(
        self.__device_selector.get_active_iter(), 1)
    offset, size, part_name = self.__partition_liststore.get(
        self.__partition_selector.get_active_iter(), 1, 2, 0)
    message = MESSAGE_FLASH % (os.path.basename(image_file), part_name)
    if confirm(self, message):
      op = None
      try:
        op = rktalk.RkOperation(self.__logger, device_info[0], device_info[1])
        op.flash_image_file(offset, size, image_file)
      finally:
        if op: del op


  def __erase_partition(self, widget):
    device_info, = self.__device_liststore.get(
        self.__device_selector.get_active_iter(), 1)
    offset, size = self.__partition_liststore.get(
        self.__partition_selector.get_active_iter(), 1, 2)
    message = MESSAGE_ERASE % (offset, size)
    if confirm(self, message):
      op = None
      try:
        op = rktalk.RkOperation(self.__logger, device_info[0], device_info[1])
        op.erase_partition(offset, size)
      finally:
        if op: del op


  def __reboot_device(self, widget):
    # Reboot device
    device_info, = self.__device_liststore.get(
        self.__device_selector.get_active_iter(), 1)
    if confirm(self, MESSAGE_REBOOT):
      op = None
      try:
        op = rktalk.RkOperation(self.__logger, device_info[0], device_info[1])
        op.reboot()
      finally:
        if op: del op


  def __check_devices(self):
    device_uids, device_list = rktalk.list_devices()
    print device_uids, self.__device_uids
    if device_uids != self.__device_uids:
      self.__device_uids = device_uids
      self.__device_liststore.clear()
      for bus_id, dev_id, vendor_id, prod_id in device_list:
        dev_name = '0x%04x:0x%04x' % (vendor_id, prod_id)
        self.__device_liststore.append(
            [dev_name, (bus_id, dev_id, vendor_id, prod_id)])
      if len(device_list) == 1:
        self.__device_selector.set_active(0)

    self.__refresh_buttons()
    return True


  def __on_device_changed(self, widget):
    self.__partition_liststore.clear()
    if self.__device_selector.get_active() > -1:
      # Read partitions
      device_info, = self.__device_liststore.get(
          self.__device_selector.get_active_iter(), 1)
      op = None
      try:
        op = rktalk.RkOperation(self.__logger, device_info[0], device_info[1])
        partitions = op.load_partitions()
        for size, offset, name in partitions:
          self.__partition_liststore.append(
              ['%s (%s @ %s)' % (name, size, offset),
               int(offset, 16), int(size, 16)])
      finally:
        if op: del op

    self.__refresh_buttons()


  def __refresh_buttons(self, *args, **kwargs):
    device_ready = self.__device_selector.get_active() > -1
    partition_ready = self.__partition_selector.get_active() > -1
    image_file = self.__image_entry.get_text().strip()
    image_ready = len(image_file) > 10 and os.path.exists(image_file)
    self.__flash_button.set_sensitive(
        device_ready and partition_ready and image_ready)
    self.__erase_button.set_sensitive(device_ready and partition_ready)
    self.__reboot_button.set_sensitive(device_ready)


class Application(object):
  def main(self):
    window = MainWindow()
    gtk.main()

