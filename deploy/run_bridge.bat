@echo off
rem Starts the Arduino serial bridge from the project root.
rem Requires the backend to be running and the Arduino plugged in.
cd /d "%~dp0.."
python -m simulator.serial_bridge
