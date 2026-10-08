#!/bin/bash

# 1. Stop any currently running instance of the chatbot to free up the port
pkill -f "python main.py"
sleep 2

# 2. Get the exact directory where this script is located
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

# 3. Source the conda initialization script dynamically
# (Cron often has a limited PATH, so we search common default installation paths)
CONDA_PATHS=(
    "$HOME/miniconda3/etc/profile.d/conda.sh"
    "$HOME/anaconda3/etc/profile.d/conda.sh"
    "$HOME/miniforge3/etc/profile.d/conda.sh"
    "/opt/miniconda3/etc/profile.d/conda.sh"
    "/opt/anaconda3/etc/profile.d/conda.sh"
)

for path in "${CONDA_PATHS[@]}"; do
    if [ -f "$path" ]; then
        source "$path"
        break
    fi
done

# 4. Try to activate preferred environments, fallback to base if they fail
if conda activate chatbot 2>/dev/null; then
    echo "Activated environment: chatbot"
elif conda activate multiagent 2>/dev/null; then
    echo "Activated environment: multiagent"
else
    echo "Falling back to base environment"
fi

# 5. Run the python script in the background and save the logs
nohup python main.py > chatbot.log 2>&1 &
