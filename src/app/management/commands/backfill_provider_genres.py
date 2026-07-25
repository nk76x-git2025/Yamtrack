import time
from dataclasses import dataclass

from django.core.management.base import BaseCommand, CommandError
from django.db import OperationalError
from django.db.models import Exists, OuterRef

from app.models import Item, MediaTypes, ProviderGenre, Sources
from app.provider_genres import (
    genres_from_metadata,
    synchronize_provider_genres,
)
from app.providers import services

LOCK_RETRY_DELAYS = (1, 2, 4)
DEFAULT_BATCH_SIZE = 25
DEFAULT_REQUEST_DELAY = 1.0


@dataclass
class BackfillStats:
    """Mutable accounting for one backfill run."""

    successes: int = 0
    skips: int = 0
    failures: int = 0
    provider_requests: int = 0
    lock_retries: int = 0
    processed: int = 0
    last_attempted_id: int | None = None
    last_completed_id: int | None = None


class Command(BaseCommand):
    """Populate stored provider genres for existing items."""

    help = "Fetch and store provider genres for existing tracked Item records."

    def add_arguments(self, parser):
        """Add resume, batching, throttling, and refresh options."""
        parser.add_argument(
            "--batch-size",
            type=int,
            default=DEFAULT_BATCH_SIZE,
            help=(
                "Number of Item rows loaded per database batch "
                f"(default: {DEFAULT_BATCH_SIZE})."
            ),
        )
        parser.add_argument(
            "--limit",
            type=int,
            help="Maximum number of Item rows to inspect.",
        )
        parser.add_argument(
            "--start-after",
            type=int,
            help="Only process Item rows with an ID greater than ITEM_ID.",
            metavar="ITEM_ID",
        )
        parser.add_argument(
            "--delay",
            type=float,
            default=DEFAULT_REQUEST_DELAY,
            help=(
                "Seconds to wait between provider requests "
                f"(default: {DEFAULT_REQUEST_DELAY:g})."
            ),
        )
        parser.add_argument(
            "--force-refresh",
            action="store_true",
            help="Refresh Items that already have stored provider genres.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Fetch metadata and report changes without writing genre data.",
        )

    def handle(self, *_args, **options):
        """Backfill genres while continuing past individual failures."""
        self._validate_options(options)
        started_at = time.monotonic()
        stats = BackfillStats()
        processed_tv_families = set()
        queryset = self._base_queryset(options["start_after"])
        total_selected = self._selected_count(queryset, options["limit"])
        interrupted = False

        self.stdout.write(
            f"Selected {total_selected} Item(s) in stable Item ID order.",
        )
        try:
            for number, item in enumerate(
                self._selected_items(
                    queryset,
                    total_selected,
                    options["batch_size"],
                ),
                start=1,
            ):
                stats.last_attempted_id = item.id
                self._process_item(
                    item,
                    number,
                    total_selected,
                    options,
                    stats,
                    processed_tv_families,
                )
                stats.processed += 1
                stats.last_completed_id = item.id
        except KeyboardInterrupt:
            interrupted = True
            self.stderr.write(
                "\nInterrupted; completed database writes were preserved.",
            )

        self._write_summary(
            stats,
            started_at,
            interrupted=interrupted,
            dry_run=options["dry_run"],
        )
        if interrupted:
            self._write_resume_command(
                options,
                stats,
                total_selected,
            )

    def _validate_options(self, options):
        """Reject unsafe or nonsensical command options."""
        if options["batch_size"] < 1:
            msg = "--batch-size must be at least 1."
            raise CommandError(msg)
        if options["limit"] is not None and options["limit"] < 1:
            msg = "--limit must be at least 1."
            raise CommandError(msg)
        if options["start_after"] is not None and options["start_after"] < 0:
            msg = "--start-after must be zero or greater."
            raise CommandError(msg)
        if options["delay"] < 0:
            msg = "--delay must be zero or greater."
            raise CommandError(msg)

    def _base_queryset(self, start_after):
        """Build the stable provider-backed Item queryset."""
        has_provider_genres = ProviderGenre.objects.filter(items=OuterRef("pk"))
        queryset = (
            Item.objects.exclude(source=Sources.MANUAL.value)
            .exclude(
                media_type__in=[
                    MediaTypes.EPISODE.value,
                    MediaTypes.EXPERIENCE.value,
                ],
            )
            .annotate(has_provider_genres=Exists(has_provider_genres))
            .order_by("id")
        )
        if start_after is not None:
            queryset = queryset.filter(id__gt=start_after)
        return queryset

    def _selected_count(self, queryset, limit):
        """Return the number of rows selected for this run."""
        count = queryset.count()
        return min(count, limit) if limit is not None else count

    def _selected_items(self, queryset, total_selected, batch_size):
        """Yield closed-cursor batches using stable ID keyset pagination."""
        remaining = total_selected
        last_id = None
        while remaining:
            batch_queryset = queryset
            if last_id is not None:
                batch_queryset = batch_queryset.filter(id__gt=last_id)
            batch = list(batch_queryset[: min(batch_size, remaining)])
            if not batch:
                return
            yield from batch
            last_id = batch[-1].id
            remaining -= len(batch)

    def _process_item(
        self,
        item,
        number,
        total_selected,
        options,
        stats,
        processed_tv_families,
    ):
        """Process one Item and report its result."""
        prefix = (
            f"[{number}/{total_selected}] Item {item.id} "
            f"{item.source}/{item.media_type}/{item.media_id}"
        )
        if item.has_provider_genres and not options["force_refresh"]:
            stats.skips += 1
            self.stdout.write(f"{prefix}: skipped (provider genres already stored)")
            return

        fetch_media_type, family_key = self._provider_target(item)
        if family_key in processed_tv_families:
            stats.skips += 1
            self.stdout.write(f"{prefix}: skipped (TV family already processed)")
            return
        if family_key is not None:
            processed_tv_families.add(family_key)

        try:
            self._throttle_provider_request(options["delay"], stats)
            stats.provider_requests += 1
            metadata = services.get_media_metadata(
                fetch_media_type,
                item.media_id,
                item.source,
                sync_provider_genres=False,
            )
            genres = genres_from_metadata(metadata)
            if genres is None:
                stats.skips += 1
                self.stdout.write(f"{prefix}: skipped (provider returned no genres)")
                return

            if not options["dry_run"]:
                self._synchronize_with_lock_retry(
                    fetch_media_type,
                    item,
                    metadata,
                    stats,
                )

            stats.successes += 1
            detail = f"{len(genres)} genre(s)"
            if options["dry_run"]:
                detail += ", dry run"
            self.stdout.write(self.style.SUCCESS(f"{prefix}: synced ({detail})"))
        except Exception as error:  # noqa: BLE001
            stats.failures += 1
            self.stderr.write(self.style.ERROR(f"{prefix}: failed ({error})"))

    def _provider_target(self, item):
        """Return the fetch type and optional TV-family deduplication key."""
        if item.media_type in {
            MediaTypes.TV.value,
            MediaTypes.SEASON.value,
        }:
            return MediaTypes.TV.value, (item.source, item.media_id)
        return item.media_type, None

    def _throttle_provider_request(self, delay, stats):
        """Wait before a subsequent real provider request."""
        if stats.provider_requests and delay:
            time.sleep(delay)

    def _synchronize_with_lock_retry(self, media_type, item, metadata, stats):
        """Retry only SQLite lock failures around the short write transaction."""
        for retry_number in range(len(LOCK_RETRY_DELAYS) + 1):
            try:
                return synchronize_provider_genres(
                    media_type,
                    item.media_id,
                    item.source,
                    metadata,
                )
            except OperationalError as error:
                is_lock_error = "database is locked" in str(error).casefold()
                if not is_lock_error or retry_number == len(LOCK_RETRY_DELAYS):
                    raise

                retry_delay = LOCK_RETRY_DELAYS[retry_number]
                stats.lock_retries += 1
                self.stderr.write(
                    self.style.WARNING(
                        f"Item {item.id}: database lock retry "
                        f"{retry_number + 1}/{len(LOCK_RETRY_DELAYS)} "
                        f"in {retry_delay} second(s)",
                    ),
                )
                time.sleep(retry_delay)
        return None

    def _write_summary(self, stats, started_at, *, interrupted, dry_run):
        """Print final accounting for normal and interrupted runs."""
        mode = "INTERRUPTED" if interrupted else "DRY RUN" if dry_run else "COMPLETE"
        elapsed = time.monotonic() - started_at
        self.stdout.write(
            f"{mode}: successes={stats.successes}, skips={stats.skips}, "
            f"failures={stats.failures}, provider requests={stats.provider_requests}, "
            f"database lock retries={stats.lock_retries}, elapsed={elapsed:.1f}s, "
            f"last completed Item ID={stats.last_completed_id or 'none'}",
        )
        if interrupted:
            self.stdout.write(
                f"Last attempted Item ID: {stats.last_attempted_id or 'none'}",
            )

    def _write_resume_command(self, options, stats, total_selected):
        """Print an exact command that resumes after the last completed row."""
        resume_after = stats.last_completed_id
        if resume_after is None:
            resume_after = options["start_after"] or 0
        command = [
            "python manage.py backfill_provider_genres",
            f"--start-after {resume_after}",
            f"--batch-size {options['batch_size']}",
            f"--delay {options['delay']:g}",
        ]
        if options["limit"] is not None:
            remaining = total_selected - stats.processed
            if remaining:
                command.append(f"--limit {remaining}")
        if options["force_refresh"]:
            command.append("--force-refresh")
        if options["dry_run"]:
            command.append("--dry-run")
        self.stdout.write(f"Resume command: {' '.join(command)}")
