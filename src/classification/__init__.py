"""
Tender industry classification (one-off / on-demand analysis).

Completely separate from the daily monitoring pipeline: nothing in this
package is imported by pipeline.py, weekly_report.py, or the dashboard. It
reads the tenders table read-only and writes only to its own cache table.
"""
