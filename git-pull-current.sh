#!/bin/bash

# Pull Current Branch
git pull origin $(git branch --show-current)

# Show the git status
git status
