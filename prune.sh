#!/bin/bash
#To stop and delete all docker volumes, containers, network etc.. 
docker compose down
docker system prune -f -a
docker compose up -d
echo "All guru associated docker services removed"
echo "=========================================="
