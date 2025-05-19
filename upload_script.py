import git
import os
from datetime import datetime

# Define repository path and file paths
repo_path = '/home/pi/craigslist_alert'
file_path = '/home/pi/craigslist_alert/craigslist_data/listings_active.csv'
commit_message = f"Update listings_active.csv - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

# Initialize repository
repo = git.Repo(repo_path)

# Stage the file
repo.git.add(file_path)

# Commit the changes
repo.index.commit(commit_message)

# Push to the remote repository
origin = repo.remote(name='origin')
origin.push()
