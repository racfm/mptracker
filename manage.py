#!/usr/bin/env python


def main():
    from mptracker.app import manager
    manager.run()


if __name__ == '__main__':
    import logging
    logging.basicConfig(loglevel=logging.INFO)
    main()

else:
    from mptracker.app import create_app
    app = create_app()
