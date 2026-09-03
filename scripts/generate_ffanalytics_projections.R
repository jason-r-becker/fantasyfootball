#!/usr/bin/env Rscript

# Generate the raw.csv inputs used by clean_data.py without using the FFA
# Shiny application. Raw projected stats are downloaded once, cached, and then
# scored locally for every league in data/<year>/scoring_profiles.json.

args <- commandArgs(trailingOnly = TRUE)

usage <- paste(
  "Usage: Rscript scripts/generate_ffanalytics_projections.R [options]",
  "",
  "Options:",
  "  --year=YEAR        Season year (defaults to the current year)",
  "  --sources=A,B,...  Projection sources (defaults to CBS, ESPN,",
  "                     FantasyPros, and FFToday)",
  "  --refresh          Discard the matching package cache and scrape again",
  "  -h, --help         Show this help and exit",
  sep = "\n"
)

if (any(args %in% c("-h", "--help"))) {
  cat(usage, "\n")
  quit(save = "no", status = 0)
}

known_args <- args %in% "--refresh" |
  grepl("^--year=", args) |
  grepl("^--sources=", args)
if (any(!known_args)) {
  stop(
    "Unknown argument(s): ",
    paste(args[!known_args], collapse = ", "),
    "\n",
    usage,
    call. = FALSE
  )
}

script_arg <- grep("^--file=", commandArgs(trailingOnly = FALSE), value = TRUE)
if (length(script_arg) != 1L) {
  stop("Could not determine this script's path")
}

script_path <- normalizePath(sub("^--file=", "", script_arg), mustWork = TRUE)
repo_root <- normalizePath(file.path(dirname(script_path), ".."), mustWork = TRUE)

arg_value <- function(prefix, default = NULL) {
  match <- grep(paste0("^", prefix, "="), args, value = TRUE)
  if (length(match) == 0L) {
    return(default)
  }
  sub(paste0("^", prefix, "="), "", match[[1]])
}

year <- as.integer(arg_value("--year", format(Sys.Date(), "%Y")))
refresh <- "--refresh" %in% args
sources <- strsplit(
  arg_value("--sources", "CBS,ESPN,FantasyPros,FFToday"),
  ",",
  fixed = TRUE
)[[1]]
positions <- c("QB", "RB", "WR", "TE")

if (!requireNamespace("ffanalytics", quietly = TRUE)) {
  stop(
    "The ffanalytics R package is not installed. Install it with: ",
    "remotes::install_github('FantasyFootballAnalytics/ffanalytics')"
  )
}
if (!requireNamespace("jsonlite", quietly = TRUE)) {
  stop("The jsonlite R package is required")
}

year_dir <- file.path(repo_root, "data", year)
profile_path <- file.path(year_dir, "scoring_profiles.json")
cache_path <- file.path(year_dir, "ffanalytics_scrape.rds")

if (!file.exists(profile_path)) {
  stop("Missing scoring profiles: ", profile_path)
}

profiles <- jsonlite::fromJSON(profile_path, simplifyVector = FALSE)

cache_matches <- function(cache) {
  is.list(cache) &&
    identical(cache$year, year) &&
    identical(cache$sources, sources) &&
    identical(cache$positions, positions) &&
    is.list(cache$data)
}

if (file.exists(cache_path) && !refresh) {
  cache <- readRDS(cache_path)
  if (cache_matches(cache)) {
    message("Using cached projected stats: ", cache_path)
    data_result <- cache$data
  } else {
    message("Projection cache does not match this request; refreshing it")
    refresh <- TRUE
  }
} else {
  refresh <- TRUE
}

if (refresh) {
  package_cache <- ffanalytics::list_ffanalytics_cache(quiet = TRUE)
  requested_cache_names <- paste(sources, "Scrape")
  cached_sources <- intersect(requested_cache_names, package_cache$object)
  if (length(cached_sources) > 0L) {
    ffanalytics::clear_ffanalytics_cache(cached_sources)
  }
  message("FFA Shiny server requests: 0")
  message(
    "Projection source jobs requested: ",
    length(sources) * length(positions),
    " (the package enforces per-source delays)"
  )
  data_result <- ffanalytics::scrape_data(
    src = sources,
    pos = positions,
    season = year,
    week = 0
  )

  missing_positions <- setdiff(positions, names(data_result))
  if (length(missing_positions) > 0L) {
    stop("No projection results for: ", paste(missing_positions, collapse = ", "))
  }

  cache <- list(
    year = year,
    sources = sources,
    positions = positions,
    created_at = format(Sys.time(), tz = "UTC", usetz = TRUE),
    ffanalytics_version = as.character(utils::packageVersion("ffanalytics")),
    data = data_result
  )
  saveRDS(cache, cache_path)
  message("Saved projected-stat cache: ", cache_path)
}

# Saved app profiles can contain stale position-specific controls alongside an
# enabled all-position control. The app treats the enabled all-position values
# as authoritative, so remove the inactive per-position values before scoring.
normalize_scoring <- function(scoring) {
  position_names <- c("QB", "RB", "WR", "TE", "K", "DST", "DL", "LB", "DB")
  for (category in intersect(names(scoring), c("rush", "rec", "misc", "ret", "idp"))) {
    if (isTRUE(scoring[[category]]$all_pos)) {
      scoring[[category]][intersect(names(scoring[[category]]), position_names)] <- NULL
    }
  }
  scoring
}

as_named_numeric <- function(values) {
  result <- unlist(values, recursive = TRUE, use.names = TRUE)
  storage.mode(result) <- "double"
  result
}

write_league <- function(league, profile) {
  scoring <- normalize_scoring(profile$scoring)
  projection_table <- ffanalytics::projections_table(
    data_result,
    scoring_rules = scoring,
    vor_baseline = as_named_numeric(profile$vor_baseline),
    tier_thresholds = as_named_numeric(profile$tier_thresholds),
    avg_type = profile$average_type
  )

  output <- ffanalytics::add_player_info(projection_table)
  output$player <- trimws(paste(output$first_name, output$last_name))
  output$position <- output$pos
  output <- output[
    is.finite(output$points) &
      is.finite(output$floor) &
      is.finite(output$ceiling) &
      is.finite(output$sd_pts) &
      !is.na(output$player) & output$player != "",
    c("player", "position", "team", "points", "floor", "ceiling", "sd_pts")
  ]
  output <- output[order(output$points, decreasing = TRUE), ]

  league_dir <- file.path(year_dir, league)
  if (!dir.exists(league_dir)) {
    stop("Missing league directory: ", league_dir)
  }
  output_path <- file.path(league_dir, "raw.csv")
  utils::write.csv(output, output_path, row.names = FALSE, na = "")
  message(
    "Wrote ", nrow(output), " players to ", output_path,
    " (profile: ", profile$source_profile, ", aggregation: ",
    profile$average_type, ")"
  )
}

invisible(Map(write_league, names(profiles), profiles))
