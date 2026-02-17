# mmap Benchmark

This repository contains the benchmarks and scripts from the CIDR 2022 paper "Are You Sure You Want to Use MMAP in Your Database Management System?" by Andrew Crotty, Viktor Leis, and Andrew Pavlo

# andy's fork notes
first arg is the block device
16 number of threads
1 for sequential, 0 for random access
madvise hints: 0 is normal, 1 is random, 2 is sequential

Example live plot usage:
```
./mmapbench /dev/sda 16 0 0 | python3 plot.py
```
