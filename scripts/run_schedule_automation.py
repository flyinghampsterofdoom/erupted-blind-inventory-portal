"""Recovery cron intentionally performs no application/database work."""
def main():
    print('RECOVERY_DISABLED: scheduling automation is unavailable in the 0031 recovery build.')

if __name__ == '__main__':
    main()
