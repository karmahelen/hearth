# hearth

Hearth is a framework designed in Python that helps deploy personal Python apps in a desktop GUI and/or served as a webpage.

For years, I have built GUIs for my personal apps using Tkinter. I was frustrated with the styling and animation aspects that could be achieved so I looked into other methods of creating more modern/rich GUIs. I ended up deciding to take the approach where the UI would be built using html/css and the UX handled by javascript. This project was born from that path. After realizing that the UI/UX elements could be implemented using standard web technologies, Hearth was developed to facilitate generating the UI either in a desktop ("normal") mode utilizing pywebview and/or serving it as a webpage directly using Flask. While also providing the "shimming" necessary to link the javascript aspect of the UX to the underlying python code of the app.

HearthMonitor is the GUI interface of the Hearth framework for managing the Hearth ecosystem. It provides a way to configure settings for how Hearth runs and dealing with the personal apps that have been developed under this framework.

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

NOTE: If you decided not to install UV and manage the python packages manually, then you can just double-click on hearthmonitor.py (or run ./hearthmonitor.py from terminal). Make sure to go to the settings (cogwheel button in top-left corner) and un-check "Run using uv".

Everything is self-contained to the folder you install to so if you don't like it you can just delete the folder to remove. If you do install UV, that is doing it's own thing and you have to look at their documentation for removal/clean-up.

## App Pics
[![View App Pics](https://img.shields.io/badge/App-Pics-blue)](https://karmahelen.github.io/hearth/AppPics.html)

## Background
I started development of this project for my own personal purposes on my Linux Mint. As I started building it up, I thought that this might be worthwhile to share. As a solo developer, I have currently only been able to fully test it out on Linux Mint 22.2 Cinnamon. I believe it should work with current releases of Ubuntu and potentially other similar Linux distros. If I can strike up interest, I would love to continue developing this for a broader audience but I need feedback. You can reach out to me at:

hearth.visible772@passinbox.com
(I am using an email alias for filtering purposes and this is what I was able to create)

I am still working on better documentation to describe the functionality and features but am waiting to see if there is any real interest before spending too much effort.

Thanks for taking the time to look at this and hopefully you found something of interest!

## License
GNU GPLv3

## Support My Work
This project is open-source and free to use. If it has brought you value please consider throwing a tip in my jar.

[![Buy Me A Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-ffdd00?style=for-the-badge&logo=buy-me-a-coffee&logoColor=black)](https://www.buymeacoffee.com/karmahelen)
