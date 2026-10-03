"""Downloader provider interface (spec §5).

A provider turns (youtube url, start, end) into a local file containing at
least the requested [start, end] range, plus `segment_start`: the timestamp
(in the ORIGINAL video's timeline) of the file's first frame. Styles use
`segment_start` to locate the requested window inside the file:

    offset_in_file = requested_start - source.segment_start

Providers must raise ProviderError (never crash the app) on timeout, page
structure change, auth failure, or blocked requests.
"""
