#! /usr/bin/python
# -*- coding: utf-8 -*


import sys

import os
RKFLASHKIT_PATH = os.path.join(os.path.abspath(os.path.dirname(sys.argv[0])), 'src')


if __name__ == '__main__':
  sys.path.append(RKFLASHKIT_PATH)

  if sys.argv[1:]:
    from rkflashkit.climain import CliMain
    app = CliMain()
    sys.exit(app.main(sys.argv[1:]))

  from rkflashkit.main import Application
  app = Application()
  app.main()

