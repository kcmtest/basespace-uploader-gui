# BaseSpace FASTQ Uploader

A small Windows GUI around Illumina's BaseSpace CLI (`bs.exe`) intended for users who do not want to work from the command line.

## Features

- Authenticate with BaseSpace and select a project
- Validate FASTQ filenames and R1/R2 pairs
- Ignore invalid filenames and save a validation report
- Upload FASTQs recursively with progress and network speed
- View upload logs or cancel an active upload

## Repository contents

The GitHub repository contains only the files needed to run or build the GUI:

    BaseSpace_FASTQ_Uploader/
      .gitignore
      README.md
      basespace_uploader.py
      requirements.txt

`bs.exe` is intentionally not committed. Download the official BaseSpace CLI
separately, then place `bs.exe` beside `basespace_uploader.py` or select it with
the GUI's **Browse** button.

FASTQ files, generated reports, Python caches, build output, batch files, and
local development copies are excluded by `.gitignore`.

## First run from Python

Install Python 3.10+ on Windows.

Open PowerShell or Command Prompt in this folder:

    pip install -r requirements.txt
    python basespace_uploader.py

The application will automatically look for `bs.exe` in the same folder. You can also browse to another copy of `bs.exe`.

## Normal workflow

1. Click **Authenticate**.
2. Your browser should open the BaseSpace authentication page.
3. Complete the BaseSpace sign-in.
4. Click **Check Login** if needed.
5. Click **Refresh Projects**.
6. Select the project.
7. Click **Browse Folder** and select the directory containing FASTQs.
8. Review the FASTQ validation summary.
9. Click **START UPLOAD**.
10. Confirm the project, folder, number of FASTQs and total size.
11. Monitor upload progress and network throughput.

## BaseSpace commands used by the GUI

Authentication:

    bs.exe auth

Identity check:

    bs.exe whoami

Project discovery:

    bs.exe list projects

Upload:

    bs.exe upload dataset -p <PROJECT_ID> --skip-invalid-filenames --recursive "<FASTQ_FOLDER>"

## FASTQ validation

The GUI recognizes the standard form:

    SampleName_S1_L001_R1_001.fastq.gz
    SampleName_S1_L001_R2_001.fastq.gz

It warns about:

- missing R1
- missing R2
- non-standard FASTQ names

Validation is meant as a user-facing pre-check; `bs.exe` remains the authority for BaseSpace upload acceptance.
