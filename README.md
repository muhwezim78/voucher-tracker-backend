# Voucher Tracker Backend

A Flask-based backend for managing MikroTik hotspot vouchers with PostgreSQL database.

## 🐳 Docker Deployment

### Prerequisites
- Docker & Docker Compose installed
- Access to MikroTik router

### Quick Start

1. **Clone and configure environment:**
   ```bash
   cp .env.example .env
   # Edit .env with your actual values
   ```

2. **Start services:**
   ```bash
   docker-compose up -d
   ```

3. **Check status:**
   ```bash
   docker-compose ps
   docker-compose logs -f backend
   ```

4. **Access the API:**
   - API: http://localhost:5000
   - Health check: http://localhost:5000/health

### Docker Commands

```bash
# Build and start
docker-compose up -d --build

# View logs
docker-compose logs -f

# Stop services
docker-compose down

# Stop and remove volumes (WARNING: deletes database)
docker-compose down -v

# Restart backend only
docker-compose restart backend

# Shell into container
docker-compose exec backend bash
```

## 🔧 Local Development

### Setup
```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # Linux/Mac
venv\Scripts\activate     # Windows

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your values

# Run the application
python app.py
```

## 📡 API Endpoints

### Health & Status
- `GET /` - API info
- `GET /health` - Comprehensive health check

### Authentication
- `POST /auth/register` - Register new user
- `POST /auth/login` - User login
- `POST /auth/logout` - User logout
- `GET /auth/profile` - Get user profile

### Vouchers
- `POST /vouchers/generate` - Generate vouchers
- `GET /vouchers/<code>` - Get voucher info
- `GET /vouchers/expired` - List expired vouchers
- `POST /vouchers/batch/pdf` - Generate batch PDF

### Users
- `GET /active-users` - Get active hotspot users
- `GET /all-users` - Get all users
- `GET /users/expired` - Get expired users

### Profiles
- `GET /profiles` - Get all profiles
- `POST /profiles` - Add new profile

### Financial
- `GET /stats` - Get financial statistics

## 🔒 Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `DB_HOST` | PostgreSQL host | `localhost` |
| `DB_PORT` | PostgreSQL port | `5432` |
| `DB_NAME` | Database name | `voucher_system` |
| `DB_USER` | Database user | `postgres` |
| `DB_PASSWORD` | Database password | - |
| `MIKROTIK_HOST` | MikroTik router IP | `192.168.88.1` |
| `MIKROTIK_USERNAME` | Router username | `admin` |
| `MIKROTIK_PASSWORD` | Router password | - |
| `APP_SECRET_KEY` | Flask secret key | - |
| `JWT_SECRET_KEY` | JWT signing key | - |

## 📁 Project Structure

```
voucher-tracker-backend/
├── app.py                 # Main Flask application
├── config.py              # Configuration management
├── Dockerfile             # Docker image definition
├── docker-compose.yml     # Docker services orchestration
├── requirements.txt       # Python dependencies
├── models/
│   └── schemas.py         # Data models
├── routes/
│   ├── auth.py            # Authentication routes
│   ├── vouchers.py        # Voucher management
│   ├── users.py           # User management
│   └── ...
├── services/
│   ├── database_service.py    # PostgreSQL operations
│   ├── mikrotik_manager.py    # MikroTik API client
│   ├── voucher_service.py     # Voucher business logic
│   ├── monitoring_service.py  # Background monitoring
│   └── auth_service.py        # Authentication logic
└── utils/
    ├── helpers.py         # Utility functions
    └── validators.py      # Input validation
```

## 🛡️ Security Notes

1. **Change default secrets** in production
2. **Use strong passwords** for database and MikroTik
3. **Enable HTTPS** with a reverse proxy (nginx/traefik)
4. **Restrict CORS origins** to your frontend domain
5. **Use environment variables** for all sensitive data
