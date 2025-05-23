import yaml
import os
from models import *
import logging
import sys
import requests
import re
import dateutil.parser
import datetime
import json
import calendar
import exceptions
import geoip2.database
from sqlite3 import connect

class _Config:
    _instance = None

    ALLOWED_ENV = ('LOG_NAME_PATTERN', 'ROBOTS_URL', 'MACHINES_URL', 'YEAR_MONTH',
        'OUTPUT_FILE', 'PLATFORM', 'HUB_API_TOKEN', 'HUB_BASE_URL', 'UPLOAD_TO_HUB',
        'SIMULATE_DATE', 'MAXMIND_GEOIP_COUNTRY_PATH', 'OUTPUT_VOLUME', 'CLEAN_FOR_RERUN',
        'PRAGMA_CACHE_SIZE', 'MAX_IDS_PER_REPORT_FILE')

    logging.basicConfig(format='%(message)s', level=logging.INFO)
    # thismodule = sys.modules[__name__]  # not sure this is needed
    def __init__(self):
        self.log = logging.getLogger(__name__)
        # things that come from the configuration file
        self.robots_reg = None
        self.machines_reg = None
        self.hit_type_reg = None
        self.log_name_pattern = None
        self.robots_url = None
        self.machines_url = None
        self.year_month = None
        self.output_file = None
        self.platform = None
        self.hub_api_token = None
        self.hub_base_url = None
        self.upload_to_hub = None
        self.simulate_date = None
        self.maxmind_geoip_country_path = None
        self.output_volume = None
        self.clean_for_rerun = None
        self.max_ids_per_report_file = None

        # things that are stored or calculated separately
        self.start_date = None
        self.end_date = None
        self.last_p_day = None
        self.run_date = None
        self.robots_reg = None
        self.state_dict = None
        self.config_file = None
        self.dsr_release = None
        self.processing_database = None
        self._memory_database = None
        self.last_processed_date = None
        self.pragma_cache_size = None

        # --- main setup and reading of all the config information ---
        self.state_dict = _Config.read_state()

        # this makes easy way to completely change the config file to a different one if needed by CONFIG_FILE ENV Variable
        self.config_file = 'config/config.yaml'
        if 'CONFIG_FILE' in os.environ:
            self.config_file = os.environ['CONFIG_FILE']

        # load the config file
        with open(self.config_file, 'r') as ymlfile:
            cfg = yaml.safe_load(ymlfile)
        for x in cfg:
            setattr(self, x, cfg[x])

        # load the secrets file if you want to separate any sensitive information from the config in secrets.yaml
        # which is .gitignored.  Anything set in secrets will override that top-level key from the config if it's set.
        secret = os.path.join(os.path.dirname(self.config_file), 'secrets.yaml')
        if os.path.isfile(secret) == True:
            with open(secret, 'r') as ymlfile:
                cfg = yaml.safe_load(ymlfile)
                for x in cfg:
                    setattr(self, x, cfg[x])


        # if someone has set any of these environment variables, overide whatever loaded from yaml (but make them lowercase props)
        for ev in self.ALLOWED_ENV:
            if ev in os.environ:
                setattr(self, ev.lower(), os.environ[ev])

        if isinstance(self.upload_to_hub, str):
            self.upload_to_hub = (self.upload_to_hub.lower() == 'true')

        if isinstance(self.output_volume, str):
            self.output_volume = (self.output_volume.lower() == 'true')

        if isinstance(self.clean_for_rerun, str):
            self.clean_for_rerun = (self.clean_for_rerun.lower() == 'true')

        # simulate date, in case someone wants to simulate running on a day besides now
        if self.simulate_date is not None:
            if isinstance(self.simulate_date, str):
                self.run_date = datetime.datetime.strptime(self.simulate_date, '%Y-%m-%d')
            else:
                self.run_date = datetime.datetime.combine(self.simulate_date, datetime.datetime.min.time())
        else:
            self.run_date = datetime.datetime.now()

        # parse in the start and end days now
        sd, ed = _Config.make_start_and_end(self.year_month)
        self.start_date = dateutil.parser.parse(sd)
        self.end_date = dateutil.parser.parse(ed)

        # set up database path
        self.processing_database = f'state/counter_db_{self.year_month}.sqlite3'

        self.copy_db_to_memory()

        base_model.deferred_db.init('file::memory:?cache=shared', uri=True)

        # set up MaxMind geoip database path.  We use binary one downloaded from https://dev.maxmind.com/geoip/geoip2/geolite2/
        self.geoip_reader = geoip2.database.Reader(self.maxmind_geoip_country_path)

        self.dsr_release = 'RD1'

    # --- reads the state from the json for the state, set in <application>/state location, static method
    def read_state():
        """State is a json file for the state of what has run.  Returns the dictionary
        from the file like {'2018-03': {'id': '2018-3-Dash', 'last_processed_day': 17}}"""
        my_dir = "state"
        if not os.path.exists(my_dir):
            os.makedirs(my_dir)

        my_file = f'{my_dir}/statefile.json'
        if not os.path.isfile(my_file):
            with open(my_file, 'w') as f:
                json.dump({}, f, sort_keys = True, indent = 4, ensure_ascii=False)

        with open(my_file) as f:
            return json.load(f)

    # static method to make start and end dates
    def make_start_and_end(my_year_month):
        """Makes the start and end dates as yyyy-mm-dd strings for the full month reporting period"""
        yr, mnth = my_year_month.split('-')
        if len(yr) != 4 or len(mnth) != 2:
            raise ValueError('year and month must be YYYY-MM format')
        yr = int(yr)
        mnth = int(mnth)
        _, lastday = calendar.monthrange(yr,mnth)
        return (f'{yr}-{mnth}-01', f'{yr}-{mnth}-{lastday}')


    def start_time(self):
        return datetime.datetime.combine(self.start_date, datetime.datetime.min.time())

    def end_time(self):
        return datetime.datetime.combine(self.end_date, datetime.datetime.min.time()) + datetime.timedelta(days=1)

    # memoization of last day
    def last_day(self):
        """The last day available in the period, either yesterday if in same month, or else last day of month if it has passed"""
        if self.last_p_day is not None:
            return self.last_p_day
        if self.end_time() < self.run_date:
            self.last_p_day = (self.end_time() - datetime.timedelta(days=1)).strftime('%Y-%m-%d') # go 1 day back because it's at 00:00 hours the first day of the next month
        else:
            self.last_p_day = (self.run_date - datetime.timedelta(days=1)).strftime('%Y-%m-%d') # a day ago from the run date
        return self.last_p_day

    def month_complete(self):
        return (self.run_date >= self.end_time())

    # gets/memoizes the robots regexp
    def robots_regexp(self):
        """Get the list of robots/crawlers from a list that is one per line
        from the URL and make a regular expression for the detection"""
        if self.robots_reg is not None:
            return self.robots_reg
        resp = requests.get(self.robots_url)
        if resp.status_code != 200:
            raise exceptions.ApiError(f'GET {self.robots_url} failed.')
        lines = resp.text.splitlines()
        lines = [s for s in lines if not s.startswith('#')]
        self.robots_reg = re.compile('|'.join(lines))
        return self.robots_reg

    # gets/memoizes the machines regexp
    def machines_regexp(self):
        """Get the list of machines from a list that is one per line
        from the URL and make a regular expression for the detection"""
        if self.machines_reg is not None:
            return self.machines_reg
        resp = requests.get(self.machines_url)
        if resp.status_code != 200:
            raise exceptions.ApiError(f'GET {self.machines_url} failed.')
        lines = resp.text.splitlines()
        lines = [s for s in lines if not s.startswith('#')]
        self.machines_reg = re.compile('|'.join(lines))
        return self.machines_reg

    # gets/memoizes the hit-type regexp
    def hit_type_regexp(self):
        """Make hit type regular expressions for investigation vs request"""
        if self.hit_type_reg is not None:
            return self.hit_type_reg
        self.hit_type_reg = { 'investigation': re.compile( '|'.join( self.path_types['investigations']) ),
            'request': re.compile( '|'.join(self.path_types['requests']))}
        return self.hit_type_reg

    def start_sql(self):
        return self.start_time().isoformat()

    def end_sql(self):
        return self.end_time().isoformat()

    def max_ids_per_file(self):
        return int(self.max_ids_per_report_file)

    def last_processed_on(self):
        """gives string for last day it was processed for this month"""
        if self.year_month in self.state_dict and 'last_processed_day' in self.state_dict[self.year_month]:
            return f'{self.year_month}-{ "%02d" % self.state_dict[self.year_month]["last_processed_day"] }'
        else:
            return f'not processed yet for {self.year_month}'

    def filenames_to_process(self):
        """Create list of filenames to process that haven't been done yet.
        They may be from 1st of month until yesterday (or last day of month).
        Or could start from the file after last we processed until yesterday
        (or the last day of the month)."""

        # if no string of '(yyyy-mm-dd)' in pattern use as one literal filename
        if '(yyyy-mm-dd)' not in self.log_name_pattern:
            return [ self.log_name_pattern ]

        ld = int(self.last_day().split('-')[2]) # last day to process, yesterday (if in period) or end of month

        # last (previously) processed day
        if self.year_month in self.state_dict and 'last_processed_day' in self.state_dict[self.year_month]:
            to_process_from = self.state_dict[self.year_month]['last_processed_day'] + 1
        else:
            to_process_from = 1

        to_process_from_str = self.year_month + '-' + ("%02d" % to_process_from)
        print(f'To process from: {to_process_from_str}')
        if to_process_from > ld:
            return []

        return [ self.log_name_pattern.replace('(yyyy-mm-dd)', self.year_month + '-' + ("%02d" % x))
            for x in range(to_process_from, ld + 1) ]

    def delete_log_processed_date(self):
        # clean up data for this period, so it can be re-run
        if self.year_month in self.state_dict:
            self.log.info(f"Removing state: {self.year_month}")
            # remove the info from the state json
            if 'last_processed_day' in self.state_dict[self.year_month]:
              self.state_dict[self.year_month].pop('last_processed_day')
            # delete the specific database for this time period
            my_file = f'state/counter_db_{self.year_month}.sqlite3'
            if os.path.exists(my_file):
                self.log.info(f"Deleting file: {my_file}")
                os.remove(my_file)
            with open('state/statefile.json', 'w') as f:
                json.dump(self.state_dict, f, sort_keys = True, indent = 4, ensure_ascii=False)

    def update_log_processed_date(self, day):
        if self.year_month in self.state_dict:
            self.state_dict[self.year_month]['last_processed_day'] = day
        else:
            self.state_dict[self.year_month] = {'last_processed_day': day}
        with open('state/statefile.json', 'w') as f:
            json.dump(self.state_dict, f, sort_keys = True, indent = 4, ensure_ascii=False)

    def get_day_from_filename(self, filename):
        return int(re.split('(\d{4}-\d{2}-\d{2})', filename, 1)[1].split("-")[2])

    def current_id(self):
        if 'id' in self.state_dict[self.year_month]:
            return self.state_dict[self.year_month]['id']
        else:
            return None

    def write_id(self, the_id):
        self.state_dict[self.year_month]['id'] = the_id
        with open('state/statefile.json', 'w') as f:
            json.dump(self.state_dict, f, sort_keys = True, indent = 4, ensure_ascii=False)

    def write_batch_index(self, index):
        # write the count of batch files written (for restart after error)
        self.state_dict[self.year_month]['report_batch_index'] = index
        with open('state/statefile.json', 'w') as f:
            json.dump(self.state_dict, f, sort_keys = True, indent = 4, ensure_ascii=False)

    def get_batch_index(self):
        # get the count of batch files written (starts at 0 to match the index number)
        if 'report_batch_index' in self.state_dict[self.year_month]:
            return self.state_dict[self.year_month]['report_batch_index']
        else:
            return -1

    def write_upload_batch_index(self, index):
        # write the index of last batch file that was successfully uploaded (for restart after error)
        self.state_dict[self.year_month]['upload_batch_index'] = index
        with open('state/statefile.json', 'w') as f:
            json.dump(self.state_dict, f, sort_keys = True, indent = 4, ensure_ascii=False)

    def get_upload_batch_index(self):
        # get the index of the last successfully uploaded report
        if 'upload_batch_index' in self.state_dict[self.year_month]:
            return self.state_dict[self.year_month]['upload_batch_index']
        else:
            return -1

    def copy_db_to_memory(self):
        # I couldn't find a way for Peewee to initialize with a sqlite3 connection rather than a string, but this url
        # shows how to share a database when using a string if it is in the same process
        # https://stackoverflow.com/questions/15720700/can-two-processes-access-in-memory-memory-sqlite-database-concurrently
        # see also https://www.devdungeon.com/content/python-sqlite3-tutorial
        if os.path.exists(self.processing_database):
            disk_db = connect(self.processing_database)
            self._memory_database = connect('file::memory:?cache=shared', uri=True)
            disk_db.backup(self._memory_database)
            disk_db.close()
        else:
            self._memory_database = connect('file::memory:?cache=shared', uri=True)

        if self.pragma_cache_size is not None:
            cursor = self._memory_database.cursor()
            cursor.execute(f'PRAGMA cache_size = {self.pragma_cache_size};')
            cursor.execute("PRAGMA cache_size;")
            cache_size = cursor.fetchone()[0]
            # Print the cache size
            print(f"PRAGMA CACHE_SIZE: {cache_size}")

    def copy_db_to_disk(self):
        # Backup a memory database to a file
        disk_db = connect(self.processing_database)
        self._memory_database.backup(disk_db)
        disk_db.close()


# this is hiding the class behind this function and making it a singleton
def Config():
    if _Config._instance is None:
        _Config._instance = _Config()
    return _Config._instance
