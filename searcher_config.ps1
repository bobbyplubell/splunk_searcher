#!/bin/bash

# splunk api host or IP
$Env:SEARCHER_HOST="10.0.15.10"
# splunk API port
$Env:SEARCHER_PORT="8089"
# splunk api username
$Env:SEARCHER_USER="admin"
# splunk api passwd
$Env:SEARCHER_PASS="notdefault"

# $Env:mode, "atom", "csv", "json", "json_cols", "json_rows", "raw", "xml" can be used remotely
# "dump" mode can be used if the script runs on the searchhead but requires the '|dump basefilename=<filename>' splunk command
# to set an $Env:format for dump command, use '|dump basefilename=<filename> format=[raw | csv | tsv | json | xml]'
$Env:SEARCHER_EXPORT_MODE="raw"
# file path to $Env:un-merged results to
# each chunked search is saved to one file named by the search's sid in all modes except dump
# dump mode creates a folder named by the search sid and moves all dump files to the folder
$Env:SEARCHER_EXPORT_PATH="C:\\temp\\searcher_export"
# file path to $Env:merged results to
$Env:SEARCHER_MERGE_PATH="C:\\temp\\searcher_export\result.txt"

# size of chunks in float hours
# 4 hour 30 minute chunk size
$Env:SEARCHER_CHUNK_SIZE="4.5"

# number of threads to use for $Env:jobs
# if this is not set, no multi-threading will be used for exports
#$Env:SEARCHER_EXPORT_THREADS="2"

# amount of jobs to use when no limit set
$Env:SEARCHER_DEFAULT_N_JOBS="1"
# use 4 jobs between 00:00 and 1:00 and use 1 job between 1:00 and midnight
#$Env:SEARCHER_LIMITS="00:00:00 01:00:00 4 01:00:00 23:59:59.99999 1"

# set to disable confirmation prompt
#$Env:SEARCHER_NO_CONFIRM
