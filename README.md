rkflashkit
==========

rkflashkit is an open sourced (GPL v2) toolkit for flashing Linux kernel images to rk3066/rk3188 based devices. It's programmed with python and gtk2. The kernel program is translated from Galland's rkflashtool_rk3066 which is in turn based on cyteen's rk3066-rkflashtool.

rkflashkit talks to the devices through vpelletier's python-libusb1 which is a python wrapper of libusb. For convenience the python-libusb1 programs are included in rkflashkit. Also included is binary created for Ubuntu.

Before installing the deb file please install its dependency:

$ sudo apt-get install python-gtk2


Links:
    https://github.com/Galland/rkflashtool_rk3066
    https://github.com/cyteen/rk3066-rkflashtool
    https://github.com/vpelletier/python-libusb1



