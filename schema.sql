CREATE TABLE IF NOT EXISTS users (
  user_id BIGINT PRIMARY KEY,
  username VARCHAR(64) NULL,
  first_name VARCHAR(128) NULL,
  created_at DATETIME NOT NULL,
  last_seen DATETIME NOT NULL,
  total_reports INT NOT NULL DEFAULT 0,
  is_banned TINYINT(1) NOT NULL DEFAULT 0
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS jobs (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  chat_id BIGINT NOT NULL,
  user_id BIGINT NOT NULL,
  number VARCHAR(20) NOT NULL,
  total INT NOT NULL,
  done INT NOT NULL DEFAULT 0,
  success INT NOT NULL DEFAULT 0,
  failed INT NOT NULL DEFAULT 0,
  status ENUM('queued','running','done','cancelled','failed') NOT NULL DEFAULT 'queued',
  reason VARCHAR(191) NULL,
  created_at DATETIME NOT NULL,
  started_at DATETIME NULL,
  finished_at DATETIME NULL,
  INDEX idx_user (user_id, created_at),
  INDEX idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS reports (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  job_id BIGINT NOT NULL,
  number VARCHAR(20) NOT NULL,
  success TINYINT(1) NOT NULL,
  reason VARCHAR(191) NULL,
  created_at DATETIME NOT NULL,
  INDEX idx_job (job_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS login_sessions (
  id TINYINT PRIMARY KEY,
  linked TINYINT(1) NOT NULL DEFAULT 0,
  number VARCHAR(20) NULL,
  updated_at DATETIME NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
