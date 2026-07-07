# Reusable Python image for the fin-assist project.
# Used by Jenkins to run tests, and later by the daily job to run the data
# processing. Commands are passed at run time (e.g. `docker run --rm fin-assist pytest`),
# so this single image serves every Python task on the Pi.

FROM python:3.12-slim

# All work happens in /app inside the container.
WORKDIR /app

# Install dependencies first. Docker caches this layer and only re-runs it
# when the requirements files change, which keeps rebuilds fast.
# requirements-dev.txt pulls in requirements.txt too, so this one image can run
# both the app tasks AND pytest.
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt

# Copy the project code into the image.
COPY . .

# Default command if none is given. Real tasks override this at `docker run`.
CMD ["python", "-c", "print('fin-assist image ready - pass a command to run a task')"]
