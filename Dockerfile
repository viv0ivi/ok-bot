CMD ["gunicorn", "--workers", "2", "--worker-class", "gthread", "--bind", "0.0.0.0:$PORT", "script_name:application"]
