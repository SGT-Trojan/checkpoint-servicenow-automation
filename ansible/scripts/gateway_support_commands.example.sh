#!/bin/bash
# Example read-only support command list. Review and tailor before production use.
clock
uptime
fw stat
fw stat -l
fw ver
cpinfo -y all
cphaprob state
cphaprob -a if
cphaprob list
cphaprob syncstat
cpstat ha
fwaccel stat
df -h
clish -c "show version all"
clish -c "show installer status all"
clish -c "show installer packages installed"
clish -c "show interfaces overview all"
