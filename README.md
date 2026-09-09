# BaseSpace Transfer Manager

A Windows GUI for uploading and downloading sequencing files with Illumina's
BaseSpace CLI (`bs.exe`).

## Features

- Authenticate with BaseSpace and select a project
- Keep the BaseSpace path, login, and project synchronized across workflows
- Validate FASTQ filenames and R1/R2 pairs
- Ignore invalid filenames and save a validation report
- Upload FASTQs recursively with progress and network speed
- Browse and download selected project files
- Browse analysis output datasets as folders and download exact selections
- View transfer logs or cancel an active transfer

## Repository contents

The GitHub repository contains only the files needed to run or build the GUI:

    BaseSpace_FASTQ_Uploader/
      .gitignore
      README.md
      basespace_gui.py
      basespace_analysis_downloader.py
      basespace_downloader.py
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
    python basespace_gui.py

Use the left navigation to switch between **Upload FASTQs**, **Download Files**,
and **Analysis Folders**. Each tool can also be launched independently with
`python basespace_uploader.py`, `python basespace_downloader.py`, or
`python basespace_analysis_downloader.py`.

The application will automatically look for `bs.exe` in the same folder. You can also browse to another copy of `bs.exe`.

## Upload workflow

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

## Download workflow

1. Open **Download Files** and check the BaseSpace login.
2. Select a source project and output folder.
3. Enter an extension or `*`, then fetch the file list.
4. Select one or more files and click **START DOWNLOAD**.

## Analysis folder workflow

1. Open **Analysis Folders** and load or enter a project.
2. Load the analyses belonging to that project and select one.
3. Load its output datasets and choose all datasets or one dataset.
4. Select dataset folders, subfolders, or individual files.
5. Click **START DOWNLOAD**.

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
