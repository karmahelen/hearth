# hearth

![Code Status](https://github.com/karmahelen/hearth/actions/workflows/security-scans.yml/badge.svg)(https://github.com/karmahelen/hearth/security/code-scanning)

Hearth is a framework designed in Python that helps deploy personal Python apps in a desktop GUI and/or served as a webpage.

For years, I have built GUIs for my personal apps using Tkinter. I was frustrated with the styling and animation aspects that could be achieved so I looked into other methods of creating more modern/rich GUIs. I ended up deciding to take the approach where the UI would be built using html/css and the UX handled by javascript. This project was born from that path. After realizing that the UI/UX elements could be implemented using standard web technologies, Hearth was developed to facilitate generating the UI either as a "normal" desktop app (local mode) utilizing pywebview and/or serving it as a webpage (serve mode) using Flask. While also providing the "shimming" necessary to link the javascript aspect of the UX to the underlying python code of the app.

HearthMonitor is the GUI interface of the Hearth framework for managing the Hearth ecosystem. It provides a way to configure settings for how Hearth runs and dealing with the personal apps that have been developed under this framework. Check out the "HearthMonitor Pics" below to see what it looks like and get a sense of what it can do.

## Features
* Utilizes the following Python packages:
    - flask
    - pywebview
    - qtpy
    - PyQt6
    - PyQt6-WebEngine
* In order to avoid having to deal with package dependencies manually, it is built to leverage using UV (see https://docs.astral.sh/uv/getting-started/installation/). I highly recommend utilizing UV but you can manage the necessary packages yourself with other methods of course.
* With HearthMonitor, you can see a list of your personal apps that are built under this framework and perform the following per app:
    - Setup launcher options
    - Personalize what the apps are called dynamically (App Aliases)
    - Open in desktop mode and/or serve as a webpage to a port on your computer
    - Set a password for served apps

## System Dependencies
Run the following to verify your system has the necessary Qt dependencies (the script will allow you to install anything that is missing):

    curl -fsSL https://raw.githubusercontent.com/karmahelen/hearth/main/hearth-sys-depends.sh | bash

## Install
Run the following to install/update:

    curl -fsSL https://raw.githubusercontent.com/karmahelen/hearth/main/hearth-install.sh | bash

Or download and run manually:

    curl -fsSL -o hearth-install.sh https://raw.githubusercontent.com/karmahelen/hearth/main/hearth-install.sh
    bash hearth-install.sh

After installing, go to the hearthmonitor directory and run the following from terminal to get started:

    uv run hearthmonitor.py

NOTE: If you decided not to install UV and manage the python packages manually, then you can just double-click on hearthmonitor.py (or run ./hearthmonitor.py from terminal). Make sure to go to the settings (cogwheel button in top-right corner) and un-check "Run using uv".

## Uninstall
Everything is self-contained to the folder you install to so if you don't like it you can just delete the folder to remove/uninstall. If you do install UV, that is doing it's own thing and you have to look at their documentation for removal/clean-up.

## HearthMonitor Pics
[![View App Pics](https://img.shields.io/badge/App-Pics-blue)](https://karmahelen.github.io/hearth/AppPics.html)

## Hearth Apps
The following apps are available that have been built under this framework:

xlist - https://github.com/karmahelen/xlist

xnetperf - https://github.com/karmahelen/xnetperf

xnote - https://github.com/karmahelen/xnote

xpwpatchbay - https://github.com/karmahelen/xpwpatchbay

xstocks - https://github.com/karmahelen/xstocks

## Running with Docker
I have also made it so that Hearth and its apps can be ran using docker. Currently, only serve mode is supported. This was useful for me to be able to run on other OS's. I have tested on Windows 11.

To run with docker, download the hearth-docker folder and from the terminal in that directory run:

    docker compose up -d --build

Hearthmonitor will then be accessible from port 8000 from a web browser and the apps can then be served to any port in the range 8001-8050.

## Background
I started development of this project for my own personal purposes on my Linux Mint. As I started building it up, I thought that this might be worthwhile to share. As a solo developer, I have currently only been able to fully test it out on Linux Mint 22.2 Cinnamon. I believe it should work with current releases of Ubuntu and potentially other similar Linux distros. If I can strike up interest, I would love to continue developing this for a broader audience but I need feedback. You can reach out to me at:

hearth.visible772@passinbox.com
(I am using an email alias for filtering purposes and this is what I was able to create)

I am still working on better documentation to describe the functionality and features but am waiting to see if there is any real interest before spending too much effort.

Thanks for taking the time to look at this and hopefully you found something of interest!

## Security
Part of the intention of this project is to create useful apps for personal needs with a focus on keeping control of your data in your own system. I realize it is a "leap of faith" whenever you leverage other's code. I have tried to build the hearth framework and related apps such that the code is as safe and self-contained as possible. Best security practices should be followed by keeping your OS and browser up-to-date.

GitHub Actions is set up to perform CodeQL, Semgrep, and Bandit code audits. Please let me know if there are other security practices that I should leverage.

When serving apps, this framework uses Flask's development server. This is not intended to be ran publicly to the internet but only to your local network (directly or through a VPN). I built in the ability to use security cookies for access control, but also note that API calls (to/from a browser) are sent unencrypted therefore for extra security you can look into leveraging a proxy manager to take advantage of HTTPS for fully encrypting app traffic over your network.

## License
GNU GPLv3

## Support My Work
This project is open-source and free to use. If it has brought you value please consider throwing a tip in my jar.

[![Buy Me A Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-ffdd00?style=for-the-badge&logo=buy-me-a-coffee&logoColor=black)](https://www.buymeacoffee.com/karmahelen)
