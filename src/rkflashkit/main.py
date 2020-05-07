#! /usr/bin/env python3
# -*- coding: utf-8 -*

from datetime import datetime

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Pango', '1.0')

from gi.repository import GLib as glib
from gi.repository import Gtk as gtk
from gi.repository import Pango as pango
import os
from . import rktalk


DEFAULT_WINDOW_WIDTH  = 1000
DEFAULT_WINDOW_HEIGHT = 650
MESSAGE_FLASH = (
    'Are you sure to flash image file %s to partition "%s"?')
MESSAGE_COMPARE = (
    'Are you sure to compare partition "%s" with image file %s?')
MESSAGE_ERASE = 'Are you sure to erase partition %s?'
MESSAGE_REBOOT = 'Are you sure to reboot the device?'


def wrap_with_scrolled_window(widget):
  scroll = gtk.ScrolledWindow()
  scroll.add(widget)
  scroll.set_policy(gtk.PolicyType.AUTOMATIC, gtk.PolicyType.AUTOMATIC)
  scroll.set_shadow_type(gtk.ShadowType.ETCHED_IN)
  scroll.set_border_width(1)
  return scroll


def confirm(parent, message):
  dialog = gtk.Dialog(message, parent, gtk.DialogFlags.MODAL, (
      gtk.STOCK_YES, gtk.ResponseType.YES,
      gtk.STOCK_CANCEL, gtk.ResponseType.CANCEL,
  ))
  dialog.vbox.pack_start(gtk.Label(message), expand=True, fill=True, padding=0)
  dialog.vbox.show_all()

  try:
    response = dialog.run()
    if response == gtk.ResponseType.YES:
      return True
  finally:
    dialog.destroy()

  return False


class BoxFrame(gtk.Frame):
  def __init__(self, caption, opt_hbox=True, spacing=0):
    gtk.Frame.__init__(self)
    self.set_label(caption)
    if opt_hbox:
      self.__box = gtk.HBox(spacing=spacing)
    else:
      self.__box = gtk.VBox(spacing=spacing)
    self.add(self.__box)
    self.__box.set_border_width(10)


  def pack_start(self, widget, **kwargs):
    self.__box.pack_start(widget, **kwargs)


class Logger(object):
  def __init__(self, text_view, scroll_window):
    self.__text_view = text_view
    self.__text_buffer = text_view.get_buffer()
    self.__vadjustment = scroll_window.get_vadjustment()
    self.__end_mark = self.__text_buffer.create_mark(
        None, self.__text_buffer.get_end_iter(), True)
    self.__first_dividor = True
    self.__dividor_tag = self.__text_buffer.create_tag(
        'dividor', weight=pango.Weight.BOLD, foreground="#FF0000")
    self.__done_tag = self.__text_buffer.create_tag(
        'done', weight=pango.Weight.BOLD, foreground="#00FF00")
    self.__error_tag = self.__text_buffer.create_tag(
        'error', weight=pango.Weight.BOLD, foreground="#FF0000")


  def log(self, message, tag=None):
    if tag:
      self.__text_buffer.insert_with_tags(
          self.__text_buffer.get_end_iter(), message, tag)
    else:
      self.__text_buffer.insert(self.__text_buffer.get_end_iter(), message)

    size1 = self.__vadjustment.get_upper() - self.__vadjustment.get_lower()
    size2 = self.__vadjustment.get_value() + self.__vadjustment.get_page_size()
    print(size1, size2)
    if (size1 - size2) * (size1 - size2) < 3.0 * 3.0:
      # Sticky scrollbar: once the scroll bar was at the end of the text view,
      # keep it at the end when appending log messages.
      self.__text_buffer.move_mark(
          self.__end_mark, self.__text_buffer.get_end_iter())
      self.__text_view.scroll_to_mark(self.__end_mark, 0.0, False, 0.0, 0.0)

    # Make the UI responsive
    while gtk.events_pending(): gtk.main_iteration()


  def print_dividor(self):
    current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    self.log('\n============= %s ============\n\n' % current_time,
             self.__dividor_tag)


  def print_done(self):
    self.log('\tDone!\n', self.__done_tag)


  def print_error(self, message):
    self.log(message, self.__error_tag)


class MainWindow(gtk.Window):
  def __init__(self):
    gtk.Window.__init__(self)

    self.__device_uids = set([])
    self.__last_backup_dir = None

    self.set_title('RkFlashKit')
    self.set_icon(self.render_icon('rkflashkit', gtk.IconSize.MENU))
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
    box.pack_start(frame, expand=False, fill=True, padding=0)

    self.__device_liststore = gtk.ListStore(str, object)
    self.__device_selector = gtk.ComboBox.new_with_model(self.__device_liststore)
    cell = gtk.CellRendererText()
    self.__device_selector.pack_start(cell, True)
    self.__device_selector.add_attribute(cell, 'text', 0)
    self.__device_selector.connect('changed', self.__on_device_changed)
    frame.pack_start(self.__device_selector, expand=True, fill=True, padding=0)

    # NAND partition selector

    frame = BoxFrame('NAND Partitions')
    box.pack_start(frame, expand=False, fill=True, padding=0)

    self.__partition_liststore = gtk.ListStore(str, int, int)
    self.__partition_selector = gtk.ComboBox.new_with_model(self.__partition_liststore)
    cell = gtk.CellRendererText()
    self.__partition_selector.pack_start(cell, True)
    self.__partition_selector.add_attribute(cell, 'text', 0)
    self.__partition_selector.connect('changed', self.__refresh_buttons)
    frame.pack_start(self.__partition_selector, expand=True, fill=True, padding=0)

    # Image file selector

    frame = BoxFrame('Image File to Flash')
    box.pack_start(frame, expand=False, fill=True, padding=0)

    self.__image_entry = gtk.Entry()
    self.__image_entry.connect('changed', self.__refresh_buttons)
    frame.pack_start(self.__image_entry, expand=True, fill=True, padding=0)

    self.__image_select_button = gtk.Button('Choose')
    self.__image_select_button.connect('clicked', self.__choose_image_file)
    frame.pack_start(self.__image_select_button, expand=False, fill=False, padding=0)

    # Action buttons

    frame = BoxFrame('Actions', opt_hbox=False, spacing=20)
    box.pack_start(frame, expand=False, fill=True, padding=0)

    self.__flash_button = gtk.Button('Flash image')
    self.__flash_button.connect('clicked', self.__flash_image_file)
    frame.pack_start(self.__flash_button, expand=False, fill=False, padding=0)

    self.__cmp_button = gtk.Button('Compare partition with image file')
    self.__cmp_button.connect('clicked', self.__cmp_part_with_file)
    frame.pack_start(self.__cmp_button, expand=False, fill=False, padding=0)

    self.__backup_button = gtk.Button('Backup Partition')
    self.__backup_button.connect('clicked', self.__backup_partition)
    frame.pack_start(self.__backup_button, expand=False, fill=False, padding=0)

    self.__erase_button = gtk.Button('Erase Partition')
    self.__erase_button.connect('clicked', self.__erase_partition)
    frame.pack_start(self.__erase_button, expand=False, fill=False, padding=0)

    self.__reboot_button = gtk.Button('Reboot Device')
    self.__reboot_button.connect('clicked', self.__reboot_device)
    frame.pack_start(self.__reboot_button, expand=False, fill=False, padding=0)

    self.__clear_log_button = gtk.Button('Clear Log')
    self.__clear_log_button.connect('clicked', self.__clear_log)
    frame.pack_start(self.__clear_log_button, expand=False, fill=False, padding=0)


  def __create_right_ui(self, box):
    self.__log_text_view = gtk.TextView()
    self.__log_text_view.set_editable(False)
    self.__log_text_view.set_wrap_mode(gtk.WrapMode.CHAR)
    scroll_window = wrap_with_scrolled_window(self.__log_text_view)
    self.__logger = Logger(self.__log_text_view, scroll_window)
    box.pack_start(scroll_window, expand=True, fill=True, padding=0)


  def __on_delete_window(self, window, event):
    gtk.main_quit()
    return True


  def __clear_log(self, widget):
    self.__log_text_view.get_buffer().set_text('')


  def __choose_image_file(self, widget):
    file_chooser = gtk.FileChooserDialog(
        'Choose Image File to Flash',
        self,
        gtk.FileChooserAction.OPEN , (
            gtk.STOCK_CANCEL, gtk.ResponseType.CANCEL,
            gtk.STOCK_OK, gtk.ResponseType.OK)
    )

    current_file = self.__image_entry.get_text().strip()
    if current_file:
      file_chooser.set_current_folder(os.path.dirname(current_file))

    try:
      response = file_chooser.run()
      if response == gtk.ResponseType.OK:
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


  def __cmp_part_with_file(self, widget):
    image_file = self.__image_entry.get_text().strip()
    device_info, = self.__device_liststore.get(
        self.__device_selector.get_active_iter(), 1)
    offset, size, part_name = self.__partition_liststore.get(
        self.__partition_selector.get_active_iter(), 1, 2, 0)
    message = MESSAGE_COMPARE % (part_name, os.path.basename(image_file))
    if confirm(self, message):
      op = None
      try:
        op = rktalk.RkOperation(self.__logger, device_info[0], device_info[1])
        op.cmp_part_with_file(offset, size, image_file)
      finally:
        if op: del op


  def __choose_backup_file(self):
    file_chooser = gtk.FileChooserDialog(
        'Create a Backup File',
        self,
        gtk.FileChooserAction.SAVE, (
            gtk.STOCK_CANCEL, gtk.ResponseType.CANCEL,
            gtk.STOCK_OK, gtk.ResponseType.OK)
    )
    if self.__last_backup_dir:
      file_chooser.set_current_folder(self.__last_backup_dir)

    try:
      response = file_chooser.run()
      if response == gtk.ResponseType.OK:
        self.__last_backup_dir = os.path.dirname(file_chooser.get_filename())
        return file_chooser.get_filename()
    finally:
      file_chooser.destroy()

    return None


  def __backup_partition(self, widget):
    backup_file = self.__choose_backup_file()
    if backup_file:
      backup_file += '.backup'
      if os.path.exists(backup_file):
        if not confirm(self, 'Back file already exists, overwrite?'):
          return

      device_info, = self.__device_liststore.get(
          self.__device_selector.get_active_iter(), 1)
      offset, size = self.__partition_liststore.get(
          self.__partition_selector.get_active_iter(), 1, 2)

      op = None
      try:
        op = rktalk.RkOperation(self.__logger, device_info[0], device_info[1])
        op.backup_partition(offset, size, backup_file)
      finally:
        if op: del op


  def __erase_partition(self, widget):
    device_info, = self.__device_liststore.get(
        self.__device_selector.get_active_iter(), 1)
    offset, size, part_name = self.__partition_liststore.get(
        self.__partition_selector.get_active_iter(), 1, 2, 0)
    message = MESSAGE_ERASE % part_name
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
              ['%s (0x%08X@0x%08X)' % (name, size, offset),
               offset, size])
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
    self.__cmp_button.set_sensitive(
        device_ready and partition_ready and image_ready)
    self.__backup_button.set_sensitive(device_ready and partition_ready)
    self.__erase_button.set_sensitive(device_ready and partition_ready)
    self.__reboot_button.set_sensitive(device_ready)


class Application(object):
  def main(self):
    window = MainWindow()
    gtk.main()

