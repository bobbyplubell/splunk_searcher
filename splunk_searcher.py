#!/bin/python3

from __future__ import annotations

import logging
import signal
import gzip
import pickle

from time import sleep
from datetime import datetime, timedelta, time
from os.path import join, isdir, exists, abspath, dirname
from os import name as os_name, listdir, mkdir, makedirs, rename, getcwd
from logging.config import dictConfig
from concurrent.futures import ThreadPoolExecutor, Future

from typing import Optional, cast

try:
    from typing import TypedDict
except ImportError:
    from typing_extensions import TypedDict
from types import FrameType

from splunklib import client
from splunklib.binding import AuthenticationError
import click

# ------------------ Globals --------------------------

# valid export modes
EXPORT_MODES = ["atom", "csv", "json", "json_cols", "json_rows", "raw", "xml", "dump"]

# global logging instance
loggy: logging.Logger

# flag to help with interrupt signal handling
QUIT: bool = False
# flag to enable multiple ctrl+c behavior
NO_INTERRUPT: bool = False
# amount of times ctrl+c has been pressed
# once = attempt resume-able exit
#   (finish running chunks if chunked search or pause job, save progress)
# twice = attempt clean exit
#   (finalize running jobs instead of waiting for them to finish in chunked search)
# thrice = forceful quit()
QUIT_C: int = 0

UNIX_SPLUNK_DISPATCH = "/opt/splunk/var/run/splunk/dispatch"
NT_SPLUNK_DISPATCH = "C:\\Program Files\\Splunk\\var\\run\\splunk\\dispatch"

TIME_FORMAT = "%H:%M:%S"
TIME_FORMAT_MS = "%H:%M:%S.%f"
SPLUNK_DATETIME_FORMAT = "%m/%d/%Y:%H:%M:%S"

# initial TTL for searches, gets reduced to 1 after searches are successfully exported
# 60s * 60m * 24hr * 1d
SPLUNK_SEARCH_TTL = 60 * 60 * 24 * 1

# ------------------ Helpers --------------------------


def get_splunk_dispatch_path() -> str:
    """
    Determine Splunk dispatch path based on os name
    """
    if os_name == "nt":
        return NT_SPLUNK_DISPATCH
    else:
        return UNIX_SPLUNK_DISPATCH


def str_to_time(time_str: str) -> time:
    """
    Convert a string like 01:23:02 to a time object
    """
    if "." in time_str:
        # use microseconds format
        return datetime.strptime(time_str, TIME_FORMAT_MS).time()
    else:
        # use non-microseconds format
        return datetime.strptime(time_str, TIME_FORMAT).time()


def date_to_splunk(date: datetime) -> str:
    """convert datetime to splunk date string"""
    return date.strftime(SPLUNK_DATETIME_FORMAT)


def splunk_to_date(datestr: str) -> datetime:
    """ convert splunk date str to datetime obj"""
    return datetime.strptime(datestr, SPLUNK_DATETIME_FORMAT)


def setup_logging() -> None:
    """this returns a config dictionary for logger configuration"""
    global loggy
    level = logging.INFO
    logfile = "splunk_searcher.log"
    log_config = dict(
        version=1,
        formatters={
            "default": {
                "format": "[%(levelname)s] %(asctime)s: %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            }
        },
        handlers={
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "default",
                "level": level,
            },
            "file": {
                "class": "logging.handlers.RotatingFileHandler",
                "maxBytes": 1024 * 5,
                "backupCount": 5,
                "filename": logfile,
                "formatter": "default",
                "level": level,
            },
        },
        root={"handlers": ["console", "file"], "level": logging.DEBUG},
    )
    dictConfig(log_config)
    loggy = logging.getLogger()


def handle_sigint(signal: int, frame: Optional[FrameType]) -> None:
    """Callback to set quit flag on interrupt signal"""
    global QUIT
    global QUIT_C

    print()
    loggy.info("Received ctrl+c")

    if not NO_INTERRUPT:
        # interrupt allowed, just quit
        quit()

    QUIT = True
    QUIT_C += 1

    if QUIT_C >= 3:
        loggy.error(("Ctrl+c pressed 3 times, force quitting. This may leave jobs hanging"
            " or results un-exported and will not allow for resume."))
        quit()
    elif QUIT_C >= 2:
        loggy.warning(( "Press ctrl+c again to force quit (WARNING: "
        "this may leave jobs running on the searchhead and cannot be resumed)"))


# ------------------ State --------------------------

# object for storing full search state
class SearchOptions(TypedDict):
    """
    Stores all options for a single search (chunked or onesho)
    """
    job_limits: Optional[JobLimits]
    chunk_options: Optional[ChunkOptions]
    export_options: ExportOptions
    export_threads: Optional[int]
    progress_path: str
    singlejob_sid: Optional[str]
    search: str
    start_date: datetime
    end_date: datetime


def save_options(search_options: SearchOptions) -> None:
    """
    Save a SearchOptions object to file
    """
    loggy.info("Saving progress to %s",search_options["progress_path"])
    with open(search_options["progress_path"], "wb") as outfile:
        pickle.dump(search_options, outfile)
    loggy.info("Saved progress. Use resume command to start search where you left off.")


def load_options(path: str) -> SearchOptions:
    """
    Load a SearchOptions object from file
    """
    loggy.info("Loading progress from %s",path)
    with open(path, "rb") as infile:
        return cast(SearchOptions, pickle.load(infile))


class JobLimits(TypedDict):
    """
    object for storing job limits
    """
    default_n_jobs: int
    limits: list[JobLimit]


class JobLimit(TypedDict):
    """
    object for storing job limit by time of day
    """
    start: str
    stop: str
    jobs: int


class ChunkOptions(TypedDict):
    """
    object for storing chunking parameters
    this is saved in order to resume search progress later if necessary
    """
    chunk_size: timedelta
    # index in generated searches array to begin searching at
    start_at: int


class ExportOptions(TypedDict):
    """
    object for storing export parameters
    """
    export_path: Optional[str]
    export_mode: Optional[str]
    merge_path: Optional[str]
    encoding: str


def print_search_options(options: SearchOptions) -> None:
    """
    Print options state
    """
    if options["chunk_options"]:
        loggy.info("Chunked Search")
        if options["export_threads"]:
            loggy.info(
                "Multithreaded export enabled with %i threads.",
                options["export_threads"]
            )
    else:
        loggy.info("Single Job Search")
    loggy.info("Query: '%s'", options["search"])
    loggy.info("Start Date: %s", options["start_date"])
    loggy.info("End Date: %s", options["end_date"])
    if options["export_options"]["export_path"]:
        loggy.info(
            "Export path: %s", abspath(options["export_options"]["export_path"])
        )
    if options["export_options"]["merge_path"]:
        loggy.info(
            "Merge path: %s",abspath(options["export_options"]["merge_path"])
        )
    if options["chunk_options"]:
        loggy.info("Chunk size: %s", options["chunk_options"]["chunk_size"])
        loggy.info("Start at search: %s", options["chunk_options"]["start_at"])
    loggy.info("Progress path: %s", options["progress_path"])

    if options["job_limits"]:
        loggy.info(
            "Using %s jobs concurrently when not limited.",
                options["job_limits"]["default_n_jobs"]
        )
        for limit_ in options["job_limits"]["limits"]:
            loggy.info(
                "Limit of %s jobs between %s and %s",
                    limit_["jobs"], limit_["start"], limit_["stop"]
            )


def validate_export_options(options: ExportOptions) -> bool:
    valid = True
    if not options["export_path"] and not options["merge_path"]:
        loggy.error(
            "No export_path or merge_path defined, results would not be exported!"
        )
        valid = False

    if options["merge_path"]:
        # folder containing target merge path
        merge_dir = dirname(abspath(options["merge_path"]))
        if not exists(merge_dir):
            loggy.warning(
                "Path to merge file %s does not appear to exist, it will be created.",
                merge_dir
            )

        if exists(options["merge_path"]):
            if isdir(options["merge_path"]):
                loggy.error(
                    "merge_path %s already exists and is not a file",
                    options["merge_path"]
                )
                valid = False
            else:
                loggy.warning("File already exists at merge path, it will be appended.")

    if options["export_mode"] == "dump":
        if not exists(get_splunk_dispatch_path()):
            loggy.error(
                "Using 'dump' export-mode but Splunk dispatch folder could not be found at %s",
                get_splunk_dispatch_path()
            )
            loggy.error(("Script must run locally on search head and have "
                "permission to read dispatch directory when using 'dump' export-mode"))
            valid = False

    if options["export_path"]:
        if exists(options["export_path"]):
            if not isdir(options["export_path"]):
                loggy.error(
                    "Export path %s is not a directory.", options["export_path"]
                )

            # list files at directory name of absolute path of export path
            if len(listdir(dirname(abspath(options["export_path"])))) > 0:
                loggy.warning(
                    "Export path %s is not empty", options["export_path"]
                )
        else:
            loggy.warning(
                "Export path %s does not exist, all directories will be created",
                options["export_path"]
            )

    return valid


def validate_job_limits(job_limits: list[JobLimit]) -> bool:
    """ensure job limits are valid"""
    # sort by start time
    job_limits = sorted(
        job_limits,
        key=lambda x: str_to_time(x["start"]),
    )
    # make sure ranges don't overlap
    for limit_idx, _ in enumerate(job_limits):
        # parse as time of day
        try:
            start_time = str_to_time(job_limits[limit_idx]["start"])
            stop_time = str_to_time(job_limits[limit_idx]["stop"])
        except ValueError:
            loggy.error(
                "Error: could not parse job limits start or stop time, %s",
                job_limits[limit_idx]
            )
            return False

        # make sure stop time is after start time
        if stop_time <= start_time:
            loggy.error(
                "Error: job limit stop time is before or equal to start time, %s",
                job_limits[limit_idx]
            )
            return False

        # if not at last limit, check that ranges don't overlap
        if limit_idx < len(job_limits) - 1:
            # get start of next limit and convert to time
            next_start = str_to_time(job_limits[limit_idx + 1]["start"])
            if stop_time > next_start:
                loggy.error(
                    "Error: job limit stop time overlaps with other limit start time, %s and %s",
                    job_limits[limit_idx],
                    job_limits[limit_idx+1]
                )
                return False
    return True


def validate_search_options(options: SearchOptions) -> None:
    valid = True
    # make sure search is using earliest and latest
    if not "{earliest}" in options["search"] or not "{latest}" in options["search"]:
        valid = False
        loggy.error("Search string must contain 'earliest={earliest} latest={latest}'")

    if not validate_export_options(options["export_options"]):
        valid = False

    # make sure |dump command is in search string if dump mode in use
    if options["export_options"]["export_mode"] == "dump":
        if "|dump" not in options["search"] and "| dump" not in options["search"]:
            loggy.error(
                "Using 'dump' export-mode but search string does not appear to use the dump command"
            )
            valid = False
    else:
        if "|dump" in options["search"] or "| dump" in options["search"]:
            loggy.warning(
                "Using |dump Splunk command but export mode is %s",
                options["export_options"]["export_mode"]
            )
            loggy.info(("If you want to set the export format of the dump results,"
             "use format option like:"
             "'| dump basefilename=<filename> format=[raw | csv | tsv | json | xml]'"))

    # validate job limits if defined
    if options["job_limits"] and len(options["job_limits"]["limits"]) > 0:
        if not validate_job_limits(options["job_limits"]["limits"]):
            valid = False

    if not valid:
        quit()


# ------------------ Splunk functions --------------------------


def create_search_job(splunk_client: client.Service, search_str: str) -> client.Job:
    """Creates a search job on the given host"""
    loggy.debug("Creating search job")
    kwargs_normalsearch = {"exec_mode": "normal"}
    job = splunk_client.jobs.create(search_str, **kwargs_normalsearch)
    job.set_ttl(SPLUNK_SEARCH_TTL)
    loggy.debug("Created search job, SID: %s Search: '%s'",job.name,search_str)
    return job


# ------------------ Export functions --------------------------


def setup_export(options: ExportOptions) -> None:
    if options["export_path"] and not exists(options["export_path"]):
        loggy.info(
            "Could not find export path %s, creating dir(s)",
            options["export_path"]
        )
        makedirs(options["export_path"])

    if options["merge_path"]:
        merge_dir = dirname(abspath(options["merge_path"]))
        if not exists(merge_dir):
            makedirs(merge_dir)


def export_job(job: client.Job, options: ExportOptions) -> Optional[int]:
    """Exports the results of a job to dest"""
    result_count: Optional[int] = None

    if options["export_mode"] == "dump":  # use local dump export mode
        # find dispatch folder by OS
        splunk_dispatch_path = get_splunk_dispatch_path()

        # make sure dispatch folder exists
        if not exists(splunk_dispatch_path):
            loggy.error("Splunk dispatch folder %s does not seem to exist but output_mode is 'dump'.",
                splunk_dispatch_path
            )
            loggy.info("Script must run local to search head if using dump mode.")
            loggy.error("Job results not exported!")
            job.set_ttl(1)
            return None

        job_folder = join(splunk_dispatch_path, job.name)
        dump_folder = join(job_folder, "dump")
        if not exists(dump_folder):
            loggy.warning("Dump folder not found for SID %s",job.name)
            return None
        dump_files = listdir(dump_folder)

        if not dump_files:
            loggy.debug("No dump files found for %s.", job.name)
            job.set_ttl(1)
            return 0

        # open gzip files and append to the merge path
        if options["merge_path"]:  # read and merge dump files into merge path
            loggy.debug(
                "Merging %s dump files to %s",
                len(dump_files),
                join(options["merge_path"])
            )
            with open(options["merge_path"], "a", encoding=options["encoding"]) as all_results:
                for dump_file in dump_files:
                    loggy.debug("Merging %s", join(dump_folder, dump_file))
                    with gzip.open(join(dump_folder, dump_file), "r") as single_result:
                        results = single_result.read().decode()
                        result_count = len(results)
                        all_results.write(results)

        # move the files to export path
        if options["export_path"]:
            dest_folder = join(options["export_path"], job.name)
            mkdir(dest_folder)
            loggy.debug("Moving %s dump files from %s to %s",
                len(dump_files),
                dump_folder,
                dest_folder
            )
            # move dispatch/<sid>/dump/* to dest/<sid>/
            for dump_file in dump_files:
                loggy.debug("Moving %s", join(dump_folder, dump_file))
                rename(join(dump_folder, dump_file), join(dest_folder, dump_file))

    else:  # use api export mode
        if options["export_path"] or options["merge_path"]:
            # get results using export_mode and decode to string
            results = job.results(output_mode=options["export_mode"]).read().decode()
            result_count = len(results)

        # write results to a file with sid as name in export path
        if options["export_path"]:  
            with open(join(options["export_path"], f"{job.name}"), "w", encoding=options["encoding"]) as single_result:
                single_result.write(results)

        if options["merge_path"]:  # write results to merge file
            with open(options["merge_path"], "a", encoding=options["encoding"]) as all_results:
                all_results.write(results)
    # reduce ttl after finished exporting to avoid clogging splunk's dispatch folder
    job.set_ttl(1)
    return result_count


# ------------------ Chunked search --------------------------
def chunk_range(
    start_date: datetime, end_date: datetime, chunk_size: timedelta
) -> list[tuple[datetime, datetime]]:
    """Converts a range into a series of (start,end) date ranges in a list"""
    if end_date < start_date:
        raise ValueError("End date is before start date.")

    # check if the range is bigger than one chunk
    if (start_date + chunk_size) > end_date:
        # if not, return start to end
        loggy.warning("Chunk size larger than entire date range, using only one chunk!")
        return [(start_date, end_date)]

    # point in time
    date_iter = start_date
    chunks = []
    # iterate until at last chunk
    while date_iter + chunk_size < end_date:
        # calculate end of current chunk
        chunk_end = date_iter + chunk_size
        # add chunk to list
        chunks.append((date_iter, chunk_end))
        # increment the date_iter by one chunk_size
        date_iter += chunk_size
    chunks.append((date_iter, end_date))

    return chunks


def chunk_search(options: SearchOptions) -> list[str]:
    """converts one search str to many with separate ranges
    search string MUST include `earliest="{earliest}" latest="{latest}"`
    """
    # ensure chunk options are present
    assert options["chunk_options"]

    # ensure search is using latest/earliest
    assert "{earliest}" in options["search"]
    assert "{latest}" in options["search"]

    range_start_str = date_to_splunk(options["start_date"])
    range_end_str = date_to_splunk(options["end_date"])

    loggy.info(
        "Chunking search %s from %s to %s with chunk_size of %s",
        options["search"],
        range_start_str,
        range_end_str,
        options["chunk_options"]["chunk_size"],
    )
    # get datetime chunks
    date_chunks = chunk_range(
        options["start_date"],
        options["end_date"],
        options["chunk_options"]["chunk_size"],
    )
    searches = []
    for chunk in date_chunks:
        # convert chunk datetimes to splunk format
        start_date_str = date_to_splunk(chunk[0])
        end_date_str = date_to_splunk(chunk[1])
        # create search with formatted dates using the template search
        new_search = options["search"].format(
            earliest=start_date_str, latest=end_date_str
        )
        searches.append(new_search)
    loggy.info("Generated %s chunked searches.", len(searches))
    return searches


# ------------------ Multi job search --------------------------
def get_max_jobs(job_limits: JobLimits) -> int:
    """use the job_limits option to get amount jobs set for current hour of day"""
    if not job_limits["limits"] or len(job_limits["limits"]) == 0:
        return job_limits["default_n_jobs"]
    now = datetime.now().time()
    # for each time range job limit entry ([range_start_hour, range_end_hour], max_jobs)
    # sorting by start time
    job_limits["limits"] = sorted(
        job_limits["limits"], key=lambda x: str_to_time(x["start"])
    )
    for job_limit in job_limits["limits"]:
        # get times of day from job control structure
        start_time = str_to_time(job_limit["start"])
        stop_time = str_to_time(job_limit["stop"])

        # check if the current time is within the range defined by the limit
        if start_time <= now <= stop_time:
            amt_jobs = int(job_limit["jobs"])
            return amt_jobs
    # no mode found for current time in job_limit list
    return job_limits["default_n_jobs"]


def remove_done(running_jobs: list[client.Job]) -> list[client.Job]:
    completed_jobs: list[str] = []
    # iterate running_jobs in reverse so that removal doesn't modify next indices
    for job_idx in reversed(range(len(running_jobs))):
        job = running_jobs[job_idx]
        if job and job.is_ready() and job.is_done():
            # remove from list
            running_jobs.pop(job_idx)
            # add SID to completed list
            completed_jobs.append(job)
    return completed_jobs


def remove_done_exports(
    export_jobs: list[Future[Optional[int]]],
) -> list[Optional[int]]:
    """ Removes finished export job futures from the list and returns them """
    finished = []
    for export_job_idx in reversed(range(len(export_jobs))):
        export_job_: Future[Optional[int]] = export_jobs[export_job_idx]
        if export_job_.done():
            finished.append(export_jobs.pop(export_job_idx).result())
    return finished


def multi_job_search(
    splunk_client: client.Service, search_options: SearchOptions, searches: list[str]
) -> None:
    """ Run a search with multiple jobs at once """
    # add sigint flag to scope
    global QUIT
    global NO_INTERRUPT
    # prevent ctrl+c from killing process and use QUIT/QUIT_C flags
    NO_INTERRUPT = True

    # setup dirs for export
    setup_export(search_options["export_options"])

    # make sure chunk options and job limits are present
    assert search_options["chunk_options"]
    assert search_options["job_limits"]

    loggy.info("Running multi-job search for %s searches.", len(searches))

    # list of currently running jobs on searchhead
    running_jobs: list[client.Job] = []
    # list of paused jobs
    paused_jobs: list[client.Job] = []
    # current idx in searches list
    # load it from search_options in case search is being resumed
    searches_idx: int = search_options["chunk_options"]["start_at"]
    # start time for elapsed time stat
    search_start_time: datetime = datetime.now()
    # total num lines exported
    total_results: int = 0
    # if multithreaded export enabled
    if search_options["export_threads"]:
        # thread pool for export jobs
        export_threads: ThreadPoolExecutor = ThreadPoolExecutor(
            search_options["export_threads"]
        )
        # pending export jobs
        export_jobs: list[Future[Optional[int]]] = []
    # flag to halt adding new jobs
    quitting: bool = False
    # helpers to make the main loop more readable
    def jobs_remain() -> bool:
        if len(running_jobs) > 0:
            return True
        if len(paused_jobs) > 0:
            return True
        if search_options["export_threads"] and len(export_jobs) > 0:
            return True
        return False

    def searches_remain() -> bool:
        return searches_idx < len(searches)

    # loop until either no jobs or searches remain, or if quitting loop until no jobs remain
    # stop loop if more than 1 ctrl+c is counted
    while (jobs_remain() or (searches_remain() and not quitting)) and not QUIT_C > 1:
        # update target jobs
        target_amt_jobs: int = get_max_jobs(search_options["job_limits"])

        # remove finished jobs and export them
        finished_jobs = remove_done(running_jobs)
        if len(finished_jobs) > 0:
            cycle_result_count = 0
            for job in finished_jobs:
                if search_options["export_threads"]:
                    export_jobs.append(
                        export_threads.submit(
                            export_job, job, search_options["export_options"]
                        )
                    )
                else:
                    # export the job results and get count if any were exported
                    job_results_count = export_job(
                        job, search_options["export_options"]
                    )
                    if job_results_count:
                        cycle_result_count += job_results_count
                        total_results += job_results_count
            if cycle_result_count > 0:
                loggy.info(
                    "%s jobs finished with %s bytes exported.",
                    len(finished_jobs),
                    cycle_result_count
                )
            elif not search_options["export_threads"]:
                loggy.info("%s jobs finished with no results.", len(finished_jobs))
            else:
                loggy.info(
                    "%s search jobs finished, added to export queue.",
                    len(finished_jobs)
                )

        # check status of export jobs if using multithread export
        if search_options["export_threads"]:
            completed_export_jobs = remove_done_exports(export_jobs)
            num_exports_completed = len(completed_export_jobs)
            # if any exports finished
            if num_exports_completed > 0:
                # calculate total results from finished exports
                num_results_exported = sum(
                    [result for result in completed_export_jobs if result]
                )
                total_results += num_results_exported
                if num_results_exported > 0:
                    loggy.info(
                        "%s export jobs finished, %s bytes exported.",
                        num_exports_completed,
                        num_results_exported
                    )
                else:
                    loggy.info(
                        "%s export jobs finished with no results.",
                        num_exports_completed
                    )

        if QUIT and not quitting:
            loggy.info(
                "Attempting safe quit, finishing running/paused jobs and saving progress."
            )
            quitting = True
            # reset QUIT flag so it can be used again
            QUIT = False

        # calculate how many jobs to start
        # lowest of either amount needed to reach target or amount needed to finish searches list
        to_add = min(target_amt_jobs - len(running_jobs), len(searches) - searches_idx)
        if quitting:
            # this prevents any new jobs from being added while maintaining the job limits during safe quit
            to_add = min(to_add, len(paused_jobs))
        # if positive amount of jobs to add
        if to_add > 0:
            loggy.debug("Starting or unpausing %s new jobs", to_add)
            for _ in range(to_add):
                # prioritize unpausing jobs before adding new ones
                if len(paused_jobs) > 0:
                    # unpause and add to running list
                    loggy.debug("Unpausing job")
                    job = paused_jobs.pop()
                    job.unpause()
                    running_jobs.append(job)
                elif not quitting:  # only add jobs if not currently quitting
                    # add new job to list, incrementing idx
                    loggy.debug("Adding new job")
                    running_jobs.append(
                        create_search_job(splunk_client, searches[searches_idx])
                    )
                    searches_idx += 1
        elif to_add < 0:  # pause jobs since there are too many
            loggy.debug("Pausing %s jobs.",-to_add)
            for _ in range(-to_add):
                job = running_jobs.pop()
                job.pause()
                paused_jobs.append(job)

        # print status and sleep to wait for more jobs to finish
        elapsed = str(datetime.now() - search_start_time)
        if search_options["export_threads"]:
            loggy.info(
                "%s / %s jobs running, %s jobs paused, %s / %s completed, %s queued export jobs, %s total bytes, %s elapsed",
                len(running_jobs),
                target_amt_jobs,
                len(paused_jobs),
                searches_idx,
                len(searches),
                len(export_jobs),
                total_results,
                elapsed,
            )
        else:
            loggy.info(
                "%s / %s jobs running, %s jobs paused, %s / %s completed, %s total bytes, %s elapsed",
                len(running_jobs),
                target_amt_jobs,
                len(paused_jobs),
                searches_idx,
                len(searches),
                total_results,
                elapsed,
            )
        sleep(1)

    # second QUIT, instead of trying to finish export, just finalize the jobs
    if QUIT_C > 1:
        loggy.error(
            "Ctrl+c pressed twice, finalizing %s jobs and quitting.",
            len(running_jobs)
        )
        for job in running_jobs:
            if job:
                job.finalize()
                # reduce ttl since the job is in-complete
                job.set_ttl(1)
        loggy.info("Running jobs finalized, quitting.")
        quit()
    elif quitting and QUIT_C == 1:  # safe quit, save progress
        # save progress if process was not quit a second time
        search_options["chunk_options"]["start_at"] = searches_idx
        save_options(search_options)
    elif not quitting and QUIT_C <= 0:
        loggy.info("All jobs completed!")

    # re-allow interrupt quit when finished
    NO_INTERRUPT = False


# ------------------ Single job search --------------------------


def single_job_search(splunk_client: client.Service, options: SearchOptions) -> None:
    """ Run a single search using a single job """
    global NO_INTERRUPT
    global QUIT
    # prevent ctrl+c from killing process and use QUIT/QUIT_C flags
    NO_INTERRUPT = True

    setup_export(options["export_options"])

    job: client.Job = None

    # make sure export options are defined as expected
    assert options["export_options"]
    assert options["export_options"]["merge_path"]
    # no multithreading for single export job
    assert not options["export_threads"]

    # find the job if resuming or create it
    if options["singlejob_sid"]:
        loggy.info("Searching for job %s", options["singlejob_sid"])
        loggy.info("This may take awhile if many jobs exist on search head.")
        # allow ctrl+c quit
        NO_INTERRUPT = False
        for job_ in splunk_client.jobs.list():
            if job_.name == options["singlejob_sid"]:
                job = job_
                job.unpause()
                break
        NO_INTERRUPT = True
        if job:
            loggy.info("Found existing job %s", job.name)
        else:
            loggy.error("Could not find job %s", options["singlejob_sid"])
            quit()
    else:
        # create the search job
        job = create_search_job(
            splunk_client,
            options["search"].format(
                earliest=date_to_splunk(options["start_date"]),
                latest=date_to_splunk(options["end_date"]),
            ),
        )
        loggy.info("Created job %s", job.name)

    # wait for job to finish running
    options["singlejob_sid"] = job.name
    while (not job or not job.is_ready() or not job.is_done()) and not QUIT:
        loggy.info("Waiting for job to complete...")
        sleep(3)
    if not QUIT:  # job is done
        loggy.info(
            "Job completed, exporting to %s",
            options["export_options"]["merge_path"]
        )
        # export the job results and get count if any were exported
        result_count = export_job(job, options["export_options"])
        if result_count and result_count > 0:
            loggy.info("Exported %s results.", result_count)
        loggy.info("Finished!")
    else:  # safe quit, pause job and save progress
        job.pause()
        loggy.info("Job %s paused.", job.name)
        save_options(options)
        quit()
    # re-allow interrupt quit when finished
    NO_INTERRUPT = False


# ------------------ CLI --------------------------
# create decorator which automatically passes in the splunk client
pass_splunk_client = click.make_pass_decorator(client.Service)

def confirm() -> bool:
    """ helper for confirming options """
    answer = ""
    while answer not in ["y", "n", "yes", "no"] and not QUIT:
        answer = input("Continue? (y/n): ")
    if QUIT:  # ctrl+c pressed
        quit()
    return answer in ["y", "yes"]


@click.group("splunk_searcher")
@click.pass_context
@click.option(
    "-u",
    "--user",
    type=str,
    help="Splunk API User username",
    prompt=True,
    hide_input=False,
    required=True,
    envvar="SEARCHER_USER",
)
@click.option(
    "--passwd",
    type=str,
    help="Splunk API User password",
    prompt=True,
    hide_input=True,
    required=True,
    envvar="SEARCHER_PASS",
)
@click.option(
    "-h",
    "--host",
    type=str,
    help="Splunk API Hostname or IP",
    prompt=True,
    required=True,
    envvar="SEARCHER_HOST",
)
@click.option(
    "-p",
    "--port",
    type=int,
    help="Splunk API port (usually 8089)",
    default=8089,
    envvar="SEARCHER_PORT",
)
@click.option("--debug", is_flag=True, help="Use debug level logging", default=False)
def cli(
    ctx: click.Context, user: str, passwd: str, host: str, port: int, debug: bool
) -> None:
    """Main CLI group, sets up splunk client"""
    print("=================== SPLUNK SEARCHER ===================")
    loggy.debug("Connecting to Splunk")
    try:
        ctx.obj = client.connect(username=user, password=passwd, host=host, port=port)
    except ConnectionRefusedError:
        loggy.error("Connection refused to %s on port %s", host, port)
        quit()
    except AuthenticationError:
        loggy.error("Splunk authentication error with user %s for %s on port %s", user, host, port)
        quit()
    loggy.debug("Connected to Splunk")

    if debug:
        for handler in loggy.handlers:
            handler.setLevel(logging.DEBUG)


@cli.command("resume")
@click.option(
    "--progress-path",
    help="Path to load progress file from, defaults to loading newest",
    default="",
)
@click.option(
    "--no-confirm",
    is_flag=True,
    help="Skip confirmation of chunk parameters",
    default=False,
)
@pass_splunk_client
def resume(splunk_client: client.Service, progress_path: str, no_confirm: bool) -> None:
    """
    Resumes an existing search with a progress file
    """
    if progress_path == "":
        loggy.warning("No progress path passed, searching %s",getcwd())
        files = listdir(getcwd())
        files = sorted(
            [x for x in files if x.startswith("searcher_progress_")], reverse=True
        )
        if len(files) > 0:
            progress_path = abspath(files[0])
            loggy.info("Found progress file %s.", progress_path)
        else:
            loggy.error(
                "Could not find progress file, please pass it using the --progress-path option"
            )
            quit()
    search_options = load_options(progress_path)
    validate_search_options(search_options)
    print_search_options(search_options)
    if search_options[
        "chunk_options"
    ]:  # chunked search, rebuild searches array and confirm
        searches = chunk_search(search_options)
        if not no_confirm:
            if not confirm():
                quit()
        multi_job_search(splunk_client, search_options, searches)
    else:
        if not no_confirm:
            if not confirm():
                quit()
        # single job search
        single_job_search(splunk_client=splunk_client, options=search_options)


@cli.command("chunk-search")
@pass_splunk_client
@click.argument("SEARCH", type=str, required=True)
@click.argument("START", type=click.DateTime(), metavar="START-DATE")
@click.argument("END", type=click.DateTime(), metavar="END-DATE")
@click.option(
    "-d",
    "--export-path",
    type=str,
    default=join(getcwd(), "export"),
    envvar="SEARCHER_EXPORT_PATH",
    help="Path to export non-merged results to (./export/ by default)",
)
@click.option(
    "--merge-path",
    type=str,
    help="File path to merge results into",
    default=None,
    envvar="SEARCHER_MERGE_PATH",
)
@click.option(
    "-m",
    "--export-mode",
    help="Mode to use for results export",
    type=click.Choice(EXPORT_MODES),
    default="raw",
    envvar="SEARCHER_EXPORT_MODE",
)
@click.option(
    "-s",
    "--chunk-size",
    type=float,
    help="Chunk size in decimal hours",
    default=0.5,
    envvar="SEARCHER_CHUNK_SIZE",
)
@click.option(
    "-n",
    "--default-n-jobs",
    type=int,
    help="Default number of jobs to use when no limits are set",
    default=1,
    envvar="SEARCHER_DEFAULT_N_JOBS",
)
@click.option(
    "--progress-path",
    help="Path to save progress file to in the event of ctrl+c",
    type=str,
    default=f"searcher_progress_{datetime.now().timestamp()}.pickle",
)
@click.option(
    "--no-confirm",
    "--yes",
    is_flag=True,
    help="Skip confirmation of chunk parameters",
    default=False,
    envvar="SEARCHER_NO_CONFIRM",
)
@click.option(
    "--limit",
    type=click.Tuple(types=[str, str, int]),
    required=False,
    multiple=True,
    help=("Limit on amount of jobs by time of day. Format (HH:MM:SS or "
    "HH:MM:SS.FFFFFF): --limit START STOP N_JOBS [--limit START STOP N_JOBS [...]]"),
    envvar="SEARCHER_LIMITS",
)
@click.option(
    "--export-threads",
    type=int,
    default=None,
    help="Enable multi-threaded export and set number of threads",
    envvar="SEARCHER_EXPORT_THREADS",
)
def cli_chunk_search(
    splunk_client: client.Service,
    search: str,
    start: datetime,
    end: datetime,
    export_path: str,
    merge_path: str,
    export_mode: str,
    chunk_size: float,
    default_n_jobs: int,
    progress_path: str,
    no_confirm: bool,
    limit: list[tuple[str, str, int]],
    export_threads: int,
) -> None:
    """
    Runs a time-chunked search with multiple jobs
    """
    chunk_size_td = timedelta(hours=chunk_size)

    # parse limits from click tuple
    limits: list[JobLimit] = []
    for start_, stop_, jobs_ in limit:
        limits.append(JobLimit(start=start_, stop=stop_, jobs=jobs_))

    # build options state
    job_limits = JobLimits(default_n_jobs=default_n_jobs, limits=limits)
    coptions = ChunkOptions(chunk_size=chunk_size_td, start_at=0)
    eoptions = ExportOptions(
        export_path=abspath(export_path), export_mode=export_mode, merge_path=merge_path, encoding="utf-8"
    )
    search_options = SearchOptions(
        search=search,
        start_date=start,
        end_date=end,
        job_limits=job_limits,
        chunk_options=coptions,
        export_options=eoptions,
        progress_path=abspath(progress_path),
        singlejob_sid=None,
        export_threads=export_threads,
    )

    # valiate and print options
    validate_search_options(search_options)
    print_search_options(search_options)
    searches = chunk_search(search_options)
    loggy.info("First search: %s", searches[0])
    if not no_confirm:
        if not confirm():
            quit()

    # run the search
    multi_job_search(splunk_client, search_options, searches)


@cli.command("search")
@pass_splunk_client
@click.argument("SEARCH", type=str)
@click.argument("START", type=click.DateTime(), metavar="START-DATE")
@click.argument("END", type=click.DateTime(), metavar="END-DATE")
@click.option(
    "-d",
    "--export-path",
    type=str,
    default=join(getcwd(), "export"),
    envvar="SEARCHER_EXPORT_PATH",
    help="Directory path to export non-merged results to (./export/ by default)",
)
@click.option(
    "--merge-path",
    type=str,
    help="File path to merge results into, defaults to not merging if unset",
    default=None,
    envvar="SEARCHER_MERGE_PATH",
)
@click.option(
    "-m",
    "--export-mode",
    help="Mode to use for results export",
    type=click.Choice(EXPORT_MODES),
    default="raw",
    envvar="SEARCHER_EXPORT_MODE",
)
@click.option(
    "--no-confirm",
    "--yes",
    is_flag=True,
    help="Skip confirmation of chunk parameters",
    default=False,
    envvar="SEARCHER_NO_CONFIRM",
)
@click.option(
    "--progress-path",
    help="Path to save progress file to in the event of ctrl+c",
    type=str,
    default=f"searcher_progress_{datetime.now().timestamp()}.pickle",
)
def cli_search(
    splunk_client: client.Service,
    search: str,
    start: datetime,
    end: datetime,
    export_path: str,
    merge_path: str,
    export_mode: str,
    no_confirm: bool,
    progress_path: str,
) -> None:
    """
    Runs a single search with one job which can be paused with ctrl+c and resumed with the 'resume' command
    """
    global QUIT

    # build options state
    eoptions = ExportOptions(
        export_path=export_path, export_mode=export_mode, merge_path=merge_path, encoding='utf-8'
    )
    options = SearchOptions(
        export_options=eoptions,
        search=search,
        start_date=start,
        end_date=end,
        job_limits=None,
        chunk_options=None,
        progress_path=progress_path,
        singlejob_sid=None,
        export_threads=None,
    )

    # validate options and print
    validate_search_options(options)
    print_search_options(options)
    if not no_confirm:
        if not confirm():
            quit()

    # run the search
    single_job_search(splunk_client, options)


def main() -> None:
    cli(prog_name="splunk_searcher.py")


if __name__ == "__main__":
    # register callback for ctrl+c interrupt
    signal.signal(signal.SIGINT, handle_sigint)
    # configure loggy logger instance
    setup_logging()
    main()
