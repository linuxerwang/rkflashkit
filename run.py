#! /usr/bin/python
# -*- coding: utf-8 -*


import sys


RKFLASHKIT_PATH = 'src'


if __name__ == '__main__':
  sys.path.append(RKFLASHKIT_PATH)
  from rkflashkit.main import Application
  app = Application()
  app.main()

