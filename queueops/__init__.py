"""queueops -- queue/service reconstruction, calibration, validation and capacity simulation.

The package only reads operational logs (events) plus declared configuration
(stations, shifts, task stage sequences).  Ground-truth service parameters live
in a separate file that ONLY the verifier module is allowed to open.
"""
