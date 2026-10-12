# Decentralized P2P File Sharing System

A Python-based Peer-to-Peer (P2P) file sharing application with a graphical interface for discovering, announcing, and securely transferring files across a local network.

## Description

This project implements a fully decentralized P2P file sharing architecture with a Graphical User Interface (GUI) built using Tkinter. Developed by The Randoms for the CMP2204 Term Project, the system allows users to seamlessly host and request files within a local subnet. The application automatically handles file partitioning (splitting assets into exactly 3 chunks), dynamic peer discovery via UDP broadcasts, and file reconstruction. To ensure data security, the system features a hybrid cryptographic layer utilizing the Diffie-Hellman key exchange algorithm to generate shared secrets, which are then used to securely encrypt file chunks via the DES-ECB standard before transmission. All core modules run concurrently inside independent background daemon threads, ensuring a completely non-blocking user experience.

## Getting Started

### Dependencies

* **OS:** Windows 10/11
* **Language:** Python 3.x
* **Standard Libraries:** `tkinter`, `socket`, `threading`, `json`, `os`, `base64`, `time`, `random` (Pre-installed with Python).
* **External Libraries:** `pyDes` (Required for the secure/encrypted chunk exchange mode).

### Installing

* Download the main application script: `p2p_app.py` (or clone this repo).
* Place the script into your desired working directory.
* Install the required cryptographic library by running the following command in your terminal:
  ```bash
  pip install pyDes
  ```
  then run: `python p2p_app.py`

### Executing program

* Network Setup: If testing on a single machine (localhost), manually change the BROADCAST TARGET input field at the top of the GUI to 192.168.1.255 and click APPLY. (If it doesn't work you can change the ip to 255.255.255.255)

* Announce a File: Click the ANNOUNCER button, enter your username, and select a file to share. The program will split it and broadcast its availability.

* Start Uploader: Click the UPLOADER button to open the TCP listening server on Port 6001.

* Start Discovery: Click the DISCOVERY button to launch the UDP receiver on Port 6000 to monitor network signals.

* Download a File: Wait 5-8 seconds for the discovery log to update. Click the DOWNLOADER button, click VIEW AVALIABLE CONTENT for be able to see shared content, click INITIATE DOWNLOAD, type the base name of the file (e.g., team) or add the format for desire extension (e.g, team.txt), and select "Yes" for a secure encrypted transfer or "No" for plain text transfer.
