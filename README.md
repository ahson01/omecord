# Omegle-Style Discord Bot

## Docker Usage

### 1. Prepare your `.env` file
Create a `.env` file in the project root with the following variables:

```
DISCORD_TOKEN=your_token_here
GUILD_ID=your_guild_id
CHANNEL_ID=your_channel_id
CLEANUP_INTERVAL=300
PAIR_INTERVAL=1.0
METRICS_PORT=8000
TIMEOUT_SECONDS=120
```

### 2. Build the Docker image
```sh
docker build -t omegle-discord-bot .
```

### 3. Run the bot container
```sh
docker run -d \
  --name omegle-discord-bot \
  --env-file .env \
  -p 8000:8000 \
  omegle-discord-bot
```

- The bot will start and expose Prometheus metrics on port 8000.
- Make sure your bot token and IDs are correct in `.env`.

---

## Development
You can also run the bot locally with:
```sh
pip install -r requirements.txt
python bot.py
``` 