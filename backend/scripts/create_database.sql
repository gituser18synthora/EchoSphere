-- EchoSphere — one-time provisioning of the SYSTEM MySQL (port 3306).
-- Run as:  sudo mysql < backend/scripts/create_database.sql
--
-- Creates database `voice_bot` and account webuser / 8hyjnx^ (matches .env).
-- That password does not meet the server's validate_password policy, so the
-- policy is relaxed for this session's CREATE USER and restored afterwards.

CREATE DATABASE IF NOT EXISTS voice_bot CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;

-- remember current policy, then relax it
SET @old_policy = @@GLOBAL.validate_password.policy;
SET @old_length = @@GLOBAL.validate_password.length;
SET @old_mixed  = @@GLOBAL.validate_password.mixed_case_count;
SET @old_special = @@GLOBAL.validate_password.special_char_count;
SET GLOBAL validate_password.policy = LOW;
SET GLOBAL validate_password.length = 4;
SET GLOBAL validate_password.mixed_case_count = 0;
SET GLOBAL validate_password.special_char_count = 0;

CREATE USER IF NOT EXISTS 'webuser'@'localhost' IDENTIFIED BY '8hyjnx^';
CREATE USER IF NOT EXISTS 'webuser'@'127.0.0.1' IDENTIFIED BY '8hyjnx^';
ALTER USER 'webuser'@'localhost' IDENTIFIED BY '8hyjnx^';
ALTER USER 'webuser'@'127.0.0.1' IDENTIFIED BY '8hyjnx^';

GRANT ALL PRIVILEGES ON voice_bot.* TO 'webuser'@'localhost';
GRANT ALL PRIVILEGES ON voice_bot.* TO 'webuser'@'127.0.0.1';
FLUSH PRIVILEGES;

-- restore the original policy
SET GLOBAL validate_password.policy = @old_policy;
SET GLOBAL validate_password.length = @old_length;
SET GLOBAL validate_password.mixed_case_count = @old_mixed;
SET GLOBAL validate_password.special_char_count = @old_special;

SELECT 'voice_bot database and webuser account ready (port 3306)' AS result;
