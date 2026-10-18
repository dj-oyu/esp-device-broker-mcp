"""Capture approval policies and HOME arming have been retired.

Authenticated clients explicitly request bounded captures and read their own
retained images. Legacy policy payloads and audit history remain untouched in
SQLite; this module reads no grants and creates no execution authority.
"""
