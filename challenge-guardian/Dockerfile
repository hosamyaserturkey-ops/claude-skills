FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY guardian ./guardian
# State persists only while the container lives; the server-side high-water
# mark keeps trailing floors honest across restarts.
CMD ["python", "-m", "guardian", "--all"]
