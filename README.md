# SEMandCD_Spot-price-Electricity-Monitoring-and-Controlling-Device
___________________________________________________________________
SEM&amp;CD, short for Spot-price Electricity Monitoring and Controlling Device (or, alternatively "PörssisähköKontrolleri"), is, as the name suggests, a program meant to keep track of current market prices for electricity, and then make it possible to control your home appliances in order to save money when the electricity bills are due. Very basic, AI-generated drivel.

<img width="1865" height="978" alt="Screenshot From 2026-07-20 18-15-35" src="https://github.com/user-attachments/assets/20e07a9b-7f59-4d56-b385-28d6e1a834ea" />


## What's in here?
There are about 3-6 moving parts in the program: 1) spot-price fetcher, made possible with an API-service ("price_fetcher.py") 2) a driver for gpio pins (like Raspberry pi's or Rock64's or any other similar Singular-Board-Computers') where you'd jack in n number of relays you're intending on controlling with this program (gpio_driver.py) 3) a daemon script that ties the aforementioned price fetching and relay switching elements together ("controller.py") 4) a miniscule config file to make it easier to edit the names for your relay appliances and their respective gpio pins ("config.yaml") 5) a semi-user-friendly browser-mediated dashboard for the whole program ("dashboard.py") and 6) perhaps somewhat little-needed quick status viewer for the spot-price controller that gives the daily minimum, maximum and average prices, the relay ON/OFF status, as well as hourly prices ("status.py").

## How it works?
Basically, you've got your device that is controlled by a relay which is managed by a Raspberry pi, wherein you're running this program as a service. You can input values (c/kWh) that act as a threshold for what prices you're willing to pay for your appliances' electricity consumption. If x is your c/kWh, then <x keeps the appliance toggled "ON", whereas x> toggles the appliance "OFF". y'know, simple.

Currently, there are 2 main controller modes (with their own sub-modes) for how the system controls the relays: 

  1) main_mode: across-the-board manual threshold that automatically cuts the power OFF for your relay
     according to the threshold value you've typed in.
         -> sub_mode: a manual override that is meant to toggle the relays ON when you need to use an
     appliance, despite current market price. This can be set to either
     A) indefinitely, or
     B) for a designated time duration you think you're going to need the device for.
     
  3) main_mode: an automatic threshold controller (no minimum or maximum threshold inputting, but instead it
     automatically takes the cheapest hours of the day)
         -> sub_mode #1: Plain N cheapest  (key: cheapest_hours):  the N globally cheapest hours
         -> sub_mode #2: Fixed window      (key: window_hours [+ per_window]):  tiles the whole day into                  equal windows and picks the cheapest per window.
         -> sub_mode #3: Night/day split  (keys: night_start, night_end [+ *_window_hours, *_per_window]):
            splits the day into a night range and a day range, then tiles each into windows and picks the
            cheapest per window.

________________________________________________________________
## Installation
The system is meant for Raspberry pi OS, and the assumption is that you have a basic understanding of Debian/GNU/Linux type terminal systems. So, you should be able to put it up and running with the following 6 steps:

### 0# Clone Git
```
git clone https://github.com/Viator-Fleischer/SEMandCD_Spot-price-Electricity-Monitoring-and-Controlling-Device.git
```

### 1# Get System Packages
```
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git
```

### 2# Get Python Packages
```
pip install --break-system-packages nordpool pyyaml flask RPi.GPIO
```

(NOTE: this program was build with Nord Pool's spot price API services, so if you want to get the price_fetcher.py up and running for your own country (in case it's outside EU or where Nord Pool doesn't operate), you're going to have to edit these out and change them to some other python package provided by an equivalent or competing electricity provider). 

### 3# Copy (git clone) the files to your path directory of your own choosing

### 4# Create Systemd Services
This is in order to make the service run on boot, and keep it running indefinitely. Paste it on your terminal, just like all the commands in preceding steps. Just make sure to type in your raspberry pi's user, the working directory and execStart location for the controller.py to where you've moved all the files to, in addition to your timezone in the environment.

```
sudo tee /etc/systemd/system/spot-controller.service > /dev/null << 'EOF'
[Unit]
Description=Spot-Price Electricity Relay Controller
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=PUT_YOUR_USERNAME_HERE
WorkingDirectory=/path/to/directory
ExecStart=/usr/bin/python3 /path/to/controller.py
Restart=on-failure
RestartSec=30
TimeoutStartSec=60
StandardOutput=journal
StandardError=journal
SyslogIdentifier=spot-controller
Environment=TZ=PUT_YOUR_TIME_ZONE_HERE

[Install]
WantedBy=multi-user.target
EOF
```
(Note here that there are two services, "spot-dashboard.service" and "spot-controller.service", so do this for both.)
```
sudo tee /etc/systemd/system/spot-dashboard.service > /dev/null << 'EOF'
[Unit]
Description=Spot-Price Controller Web Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=PUT_YOUR_USERNAME_HERE
WorkingDirectory=/path/to/directory
ExecStart=/usr/bin/python3 /path/to/dashboard.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=spot-dashboard
Environment=TZ=PUT_YOUR_TIME_ZONE_HERE

[Install]
WantedBy=multi-user.target
EOF
```
### 5# Enable and Start
```
sudo systemctl daemon-reload
sudo systemctl enable spot-controller spot-dashboard
sudo systemctl start spot-controller spot-dashboard
sudo systemctl status spot-controller spot-dashboard
```
### 6# Verify (if you want to)
```
sudo journalctl -u spot-controller -n 15 --no-pager
```

(here you'll want to see something along the lines of "Loaded N price entries" and the relay status table which lists whether or not the relays are ON or OFF.

### 7# Edit the Config File
The Config.yaml file is where you'll keep your threshold modes, names of your appliances and the right number of gpio pins. These can be changed via the browser dashboard, but this is a nice option in case you're running a headless OS without a graphical user interface.
_________________________________


