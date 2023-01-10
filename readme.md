# Splunk Searcher
This script is intended to run large searches that may not work on the WebUI

# Virtualenv setup
It's highly recommended to use a virtualenv to run the script. To set one up do the following:
```
# unix
python3 -m pip install virtualenv
python3 -m venv venv
venv/bin/python3 -m pip install -r requirements.txt

# windows
python3 -m pip install virtualenv
python3 -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt
```

# Usage
The script has quite a few parameters and all are broken out on the command line. Some can be set via environment variables as in `searcher_config.ps1` and `searcher_config.sh`.

# Command line
basic configuration:
```
Usage: splunk_searcher.py [OPTIONS] COMMAND [ARGS]...

  Main CLI group, sets up splunk client

Options:
  -u, --user TEXT     Splunk API User username  [required]
  --passwd TEXT       Splunk API User password  [required]
  -h, --host TEXT     Splunk API Hostname or IP  [required]
  -p, --port INTEGER  Splunk API port (usually 8089)
  --debug             Use debug level logging
  --help              Show this message and exit.

Commands:
  chunk-search  Runs a time-chunked search with multiple jobs
  resume        Resumes an existing search with a progress file
  search        Runs a single search with one job which can be paused
```

search: single job search
```
Usage: splunk_searcher.py search [OPTIONS] SEARCH START-DATE END-DATE

  Runs a single search with one job which can be paused with ctrl+c and
  resumed with the 'resume' command

Options:
  -d, --export-path TEXT          Directory path to export non-merged results
                                  to (./export/ by default)
  --merge-path TEXT               File path to merge results into, defaults to
                                  not merging if unset
  -m, --export-mode [atom|csv|json|json_cols|json_rows|raw|xml|dump]
                                  Mode to use for results export
  --no-confirm, --yes             Skip confirmation of chunk parameters
  --progress-path TEXT            Path to save progress file to in the event
                                  of ctrl+c
  --help                          Show this message and exit.
```

chunk-search: search with multiple jobs and multiple export threads
```
Usage: splunk_searcher.py chunk-search [OPTIONS] SEARCH START-DATE END-DATE

  Runs a time-chunked search with multiple jobs

Options:
  -d, --export-path TEXT          Path to export non-merged results to
                                  (./export/ by default)
  --merge-path TEXT               File path to merge results into
  -m, --export-mode [atom|csv|json|json_cols|json_rows|raw|xml|dump]
                                  Mode to use for results export
  -s, --chunk-size FLOAT          Chunk size in decimal hours
  -n, --default-n-jobs INTEGER    Default number of jobs to use when no limits
                                  are set
  --progress-path TEXT            Path to save progress file to in the event
                                  of ctrl+c
  --no-confirm, --yes             Skip confirmation of chunk parameters
  --limit <TEXT TEXT INTEGER>...  Limit on amount of jobs by time of day.
                                  Format (HH:MM:SS or HH:MM:SS.FFFFFF):
                                  --limit START STOP N_JOBS [--limit START
                                  STOP N_JOBS [...]]
  --export-threads INTEGER        Enable multi-threaded export and set number
                                  of threads
  --help                          Show this message and exit.
  ```

# Environment variables
To use the environment variable files, they must be sourced (`source searcher_config.sh` on unix or `. .\searcher_config.sh` on windows)
```
# splunk api host or IP
export SEARCHER_HOST="localhost"
# splunk API port
export SEARCHER_PORT="8089"
# splunk api username
export SEARCHER_USER="admin"
# splunk api passwd
export SEARCHER_PASS="notdefault"

# export mode, "atom", "csv", "json", "json_cols", "json_rows", "raw", "xml" can be used remotely
# "dump" mode can be used if the script runs on the searchhead but requires the '|dump basefilename=<filename>' splunk command
# to set an export format for dump command, use '|dump basefilename=<filename> format=[raw | csv | tsv | json | xml]'
export SEARCHER_EXPORT_MODE="raw"
# file path to export un-merged results to
# each chunked search is saved to one file named by the search's sid in all modes except dump
# dump mode creates a folder named by the search sid and moves all dump files to the folder
export SEARCHER_EXPORT_PATH="/tmp/searcher_export/"
# file path to export merged results to
export SEARCHER_MERGE_PATH="/tmp/searcher_export/result.txt"

# size of chunks in float hours
# 4 hour 30 minute chunk size
export SEARCHER_CHUNK_SIZE="4.5"

# number of threads to use for export jobs
# if this is not set, no multi-threading will be used for exports
#export SEARCHER_EXPORT_THREADS="2"

# amount of jobs to use when no limit set
export SEARCHER_DEFAULT_N_JOBS="1"
# use 4 jobs between 00:00 and 1:00 and use 1 job between 1:00 and midnight
#export SEARCHER_LIMITS="00:00:00 01:00:00 4 01:00:00 23:59:59.99999 1"

# set to disable confirmation prompt
#export SEARCHER_NO_CONFIRM

```

Using environment variables allows for much simpler calls to the script. Using the above config, here are some possible commands to run searches:
```
# run a search of all data in the environment
# merge all the results into /tmp/results/result.txt with raw format
# use raw format
# use 10 jobs concurrently
# use a pool of 10 threads to move results
# chunk the search into 3 hour segments
venv/bin/python3 splunk_searcher.py chunk-search 'search * earliest={earliest} latest={latest}' 2023-01-01 2023-01-30T01:00:00 --merge-path '/tmp/results/result.txt' -m raw -n 10 
--export-threads 10 --chunk-size 3

# same as first search, except instead of merging the results, they'll be saved into individual files for each chunk under /tmp/results/
venv/bin/python3 splunk_searcher.py chunk-search 'search * earliest={earliest} latest={latest}' 2023-01-01 2023-01-30T01:00:00 --export-path '/tmp/results/' -m raw -n 10 
--export-threads 10 --chunk-size 3

# run search with single job
# place resuls in /tmp/results/result.txtt
# use raw format
venv/bin/python3 splunk_searcher.py search 'search * earliest={earliest} latest={latest}' 2023-01-01 2023-01-30T01:00:00 --merge-path '/tmp/results/result.txt' -m raw

```

# Resume functionality
Both chunk-search and search commands can be cancelled with a single ctrl+c during operation. The script will attempt to finish up any running jobs and then save the current progress to a file.
You can resume a saved search with the resume command.
```
root@splunky:/home/bobbyplubell# venv/bin/python3 splunk_searcher.py chunk-search 'search * earliest={earliest} latest={latest}' 2023-01-01 2023-01-30T01:00:00 --merge-path '/tmp/results/result.txt' -m raw -n 10 
--export-threads 10
[WARNING] 2023-01-10 00:57:12: File already exists at merge path, it will be appended.
[WARNING] 2023-01-10 00:57:12: Export path /tmp/searcher_export is not empty
[INFO] 2023-01-10 00:57:12: Chunked Search
[INFO] 2023-01-10 00:57:12: Multithreaded export enabled with 10 threads.
[INFO] 2023-01-10 00:57:12: Query: 'search * earliest={earliest} latest={latest}'
[INFO] 2023-01-10 00:57:12: Start Date: 2023-01-01 00:00:00
[INFO] 2023-01-10 00:57:12: End Date: 2023-01-30 01:00:00
[INFO] 2023-01-10 00:57:12: Export path: /tmp/searcher_export
[INFO] 2023-01-10 00:57:12: Merge path: /tmp/results/result.txt
[INFO] 2023-01-10 00:57:12: Chunk size: 4:30:00
[INFO] 2023-01-10 00:57:12: Start at search: 0
[INFO] 2023-01-10 00:57:12: Progress path: /home/bobbyplubell/searcher_progress_1673333832.420941.pickle
[INFO] 2023-01-10 00:57:12: Using 10 jobs concurrently when not limited.
[INFO] 2023-01-10 00:57:12: Chunking search search * earliest={earliest} latest={latest} from 01/01/2023:00:00:00 to 01/30/2023:01:00:00 with chunk_size of 4:30:00
[INFO] 2023-01-10 00:57:12: Generated 155 chunked searches.
[INFO] 2023-01-10 00:57:12: First search: search * earliest=01/01/2023:00:00:00 latest=01/01/2023:04:30:00
Continue? (y/n): y
[INFO] 2023-01-10 00:57:14: Running multi-job search for 155 searches.
[INFO] 2023-01-10 00:57:15: 10 / 10 jobs running, 0 jobs paused, 10 / 155 completed, 0 queued export jobs, 0 total bytes, 0:00:00.933682 elapsed
[INFO] 2023-01-10 00:57:16: 9 search jobs finished, added to export queue.
[INFO] 2023-01-10 00:57:16: 4 export jobs finished with no results.
^C
[INFO] 2023-01-10 00:57:17: Received ctrl+c
[INFO] 2023-01-10 00:57:17: 10 / 10 jobs running, 0 jobs paused, 19 / 155 completed, 5 queued export jobs, 0 total bytes, 0:00:02.952427 elapsed
[INFO] 2023-01-10 00:57:18: 10 search jobs finished, added to export queue.
[INFO] 2023-01-10 00:57:18: 5 export jobs finished with no results.
[INFO] 2023-01-10 00:57:18: Attempting safe quit, finishing running/paused jobs and saving progress.
[INFO] 2023-01-10 00:57:18: 0 / 10 jobs running, 0 jobs paused, 19 / 155 completed, 10 queued export jobs, 0 total bytes, 0:00:04.502037 elapsed
[INFO] 2023-01-10 00:57:19: 10 export jobs finished with no results.
[INFO] 2023-01-10 00:57:19: 0 / 10 jobs running, 0 jobs paused, 19 / 155 completed, 0 queued export jobs, 0 total bytes, 0:00:05.504058 elapsed
[INFO] 2023-01-10 00:57:20: Saving progress to /home/bobbyplubell/searcher_progress_1673333832.420941.pickle
[INFO] 2023-01-10 00:57:20: Saved progress. Use resume command to start search where you left off.
root@splunky:/home/bobbyplubell# venv/bin/python3 splunk_searcher.py resume
[WARNING] 2023-01-10 00:57:31: No progress path passed, searching /home/bobbyplubell
[INFO] 2023-01-10 00:57:31: Found progress file /home/bobbyplubell/searcher_progress_1673333832.420941.pickle.
[INFO] 2023-01-10 00:57:31: Loading progress from /home/bobbyplubell/searcher_progress_1673333832.420941.pickle
[WARNING] 2023-01-10 00:57:31: File already exists at merge path, it will be appended.
[WARNING] 2023-01-10 00:57:31: Export path /tmp/searcher_export is not empty
[INFO] 2023-01-10 00:57:31: Chunked Search
[INFO] 2023-01-10 00:57:31: Multithreaded export enabled with 10 threads.
[INFO] 2023-01-10 00:57:31: Query: 'search * earliest={earliest} latest={latest}'
[INFO] 2023-01-10 00:57:31: Start Date: 2023-01-01 00:00:00
[INFO] 2023-01-10 00:57:31: End Date: 2023-01-30 01:00:00
[INFO] 2023-01-10 00:57:31: Export path: /tmp/searcher_export
[INFO] 2023-01-10 00:57:31: Merge path: /tmp/results/result.txt
[INFO] 2023-01-10 00:57:31: Chunk size: 4:30:00
[INFO] 2023-01-10 00:57:31: Start at search: 19
[INFO] 2023-01-10 00:57:31: Progress path: /home/bobbyplubell/searcher_progress_1673333832.420941.pickle
[INFO] 2023-01-10 00:57:31: Using 10 jobs concurrently when not limited.
[INFO] 2023-01-10 00:57:31: Chunking search search * earliest={earliest} latest={latest} from 01/01/2023:00:00:00 to 01/30/2023:01:00:00 with chunk_size of 4:30:00
[INFO] 2023-01-10 00:57:31: Generated 155 chunked searches.
Continue? (y/n): y
[INFO] 2023-01-10 00:57:33: Running multi-job search for 155 searches.
[INFO] 2023-01-10 00:57:34: 10 / 10 jobs running, 0 jobs paused, 29 / 155 completed, 0 queued export jobs, 0 total bytes, 0:00:01.020104 elapsed
[INFO] 2023-01-10 00:57:35: 10 search jobs finished, added to export queue.
[INFO] 2023-01-10 00:57:35: 8 export jobs finished with no results.
[INFO] 2023-01-10 00:57:36: 10 / 10 jobs running, 0 jobs paused, 39 / 155 completed, 2 queued export jobs, 0 total bytes, 0:00:03.223617 elapsed
```