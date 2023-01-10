#!/bin/bash

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
