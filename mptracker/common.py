from datetime import datetime
from contextlib import contextmanager
from collections import namedtuple
import subprocess
import logging
import tempfile
import csv
import re
from io import StringIO
from itertools import chain
import flask
from werkzeug.routing import BaseConverter, ValidationError
from flask.ext.rq import job
from path import path

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

MAX_OCR_PAGES = 3

common = flask.Blueprint('common', __name__)


class UuidConverter(BaseConverter):

    def to_python(self, value):
        if re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-'
                    r'[0-9a-f]{4}-[0-9a-f]{12}$', value) is None:
            raise ValidationError
        return value


@common.record
def register_url_converters(state):
    app = state.app
    app.url_map.converters['uuid'] = UuidConverter


def parse_date(date_str):
    return datetime.strptime(date_str, '%Y-%m-%d').date()


def fix_local_chars(txt):
    return (txt.replace("ş", "ș").replace("Ş", "Ș")
               .replace("ţ", "ț").replace("Ţ", "Ț"))


@contextmanager
def temp_dir():
    tmp = path(tempfile.mkdtemp())
    try:
        yield tmp
    finally:
        tmp.rmtree()


class RowNotFound(Exception):
    """ Could not find row to match key. """


AddResult = namedtuple('AddResult', ['row', 'is_new', 'is_changed'])


class TablePatcher:

    def __init__(self, model, session, key_columns):
        self.model = model
        self.session = session
        self.key_columns = key_columns
        self.existing = {}
        for row in self.model.query:
            key = self.row_key(row)
            assert key not in self.existing, "Duplicate key %r" % key
            self.existing[key] = row
        self.seen = set()

    def row_key(self, row):
        return tuple(getattr(row, k) for k in self.key_columns)

    def dict_key(self, record):
        return tuple(record.get(k) for k in self.key_columns)

    def add(self, record, create=True):
        key = self.dict_key(record)
        row = self.existing.get(key)
        is_new = is_changed = False

        if row is None:
            if create:
                row = self.model()
                logger.info("Adding %r", key)
                is_new = is_changed = True
                self.session.add(row)
                self.existing[key] = row

            else:
                raise RowNotFound("Could not find row with key=%r" % key)

        else:
            changes = []
            for k in record:
                old_val = getattr(row, k)
                new_val = record[k]
                if old_val != new_val:
                    logger.debug("Value change for %r: %s %r != %r",
                                 key, k, old_val, new_val)
                    changes.append(k)

            if changes:
                logger.info("Updating %r %s", key, ','.join(changes))
                is_changed = True

        if is_changed:
            for k in record:
                setattr(row, k, record[k])

        self.seen.add(key)

        return AddResult(row, is_new, is_changed)

    @contextmanager
    def process(self, autoflush=None, remove=False):
        counters = {'n_add': 0, 'n_update': 0,
                    'n_remove': 0, 'n_ok': 0, 'total': 0}

        def add(record, create=True):
            result = self.add(record, create=create)

            counters['total'] += 0
            if autoflush and counters['total'] % autoflush == 0:
                self.session.flush()

            if result.is_new:
                counters['n_add'] += 1

            elif result.is_changed:
                counters['n_update'] += 1

            else:
                counters['n_ok'] += 1

            return result

        self.seen.clear()

        yield add

        if remove:
            for key in set(self.existing) - self.seen:
                self.session.delete(self.existing[key])
                logger.info("Removing %r", key)
                counters['n_remove'] += 1

        self.session.commit()
        logger.info("Created %d, updated %d, removed %d, found ok %d.",
                    counters['n_add'], counters['n_update'],
                    counters['n_remove'], counters['n_ok'])

    def update(self, data, create=True, remove=False):
        with self.process(autoflush=1000, remove=remove) as add:
            for record in data:
                add(record, create=create)


@job
def ocr_url(url, max_pages=MAX_OCR_PAGES):
    from mptracker.scraper.common import create_session

    pdf_cache_name = flask.current_app.config.get('MPTRACKER_PDF_CACHE')
    http_session = create_session(cache_name=pdf_cache_name, throttle=0.5)

    with temp_dir() as tmp:
        pdf_data = http_session.get(url).content
        pdf_path = tmp / 'document.pdf'
        with pdf_path.open('wb') as f:
            f.write(pdf_data)
        subprocess.check_call(['pdfimages', pdf_path, tmp / 'img'])

        pages = []
        for image_path in sorted(tmp.listdir('img-*'))[:MAX_OCR_PAGES]:
            subprocess.check_call(['tesseract',
                                   image_path, image_path,
                                   '-l', 'ron'],
                                  stderr=subprocess.DEVNULL)
            text = (image_path + '.txt').text()
            pages.append(text)

        return pages


def csv_lines(cols, rows):
    out = StringIO()
    writer = csv.DictWriter(out, cols)
    header = dict(zip(cols, cols))
    for r in chain([header], rows):
        writer.writerow(r)
        yield out.getvalue()
        out.seek(out.truncate(0))
