#!/usr/bin/env python3
"""Synthetic large workbook for performance tests: frozen header, merged title, styled header, number formats, a formula column.
usage: make_large_workbook.py ROWS COLS OUT.xlsx"""
import sys
import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

rows, cols, out = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Data"
ws.append(["Large synthetic workbook"] + [None] * (cols - 1)); ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=4)
ws["A1"].font = Font(bold=True, size=14); ws["A1"].alignment = Alignment(horizontal="center")
hdr = ["Label", "Count", "Amount", "Note"] + [f"M{k}" for k in range(cols - 5)] + ["Total"]
ws.append(hdr)
for c in range(1, cols + 1):
    ws.cell(row=2, column=c).font = Font(bold=True, color="FFFFFF"); ws.cell(row=2, column=c).fill = PatternFill("solid", fgColor="1F4E78")
for r in range(3, rows + 3):
    ws.append([f"Row {r - 2}", r - 2, (r - 2) * 1.5, "note"] + [(r - 2) + k for k in range(cols - 5)] + [f"=B{r}+C{r}"])
    ws.cell(row=r, column=3).number_format = "#,##0.00"
ws.freeze_panes = "B3"; ws.column_dimensions["A"].width = 16; ws.column_dimensions["D"].width = 14
for c in range(5, cols + 1):
    ws.column_dimensions[get_column_letter(c)].width = 9
ws2 = wb.create_sheet("Notes"); ws2.append(["Second sheet"]); [ws2.append([i, f"v{i}"]) for i in range(1, 400)]
wb.save(out)
print(out, "cells ~", rows * cols)
