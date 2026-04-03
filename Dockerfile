FROM python:3.12-slim

WORKDIR /app
COPY unrar /usr/local/bin/unrar
RUN chmod +x /usr/local/bin/unrar
COPY media_sorter.py ./

EXPOSE 8765

ENTRYPOINT ["python3", "media_sorter.py", "--daemon"]
