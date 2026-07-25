from io import StringIO
from unittest.mock import call, patch

from django.core.management import call_command
from django.db import OperationalError, connection
from django.test import TransactionTestCase

from app.models import Item, MediaTypes, ProviderGenre, Sources
from app.provider_genres import synchronize_provider_genres

COMMAND_PATH = "app.management.commands.backfill_provider_genres"
SUCCESSFUL_LOCK_ATTEMPT = 3


class BackfillProviderGenresCommandTests(TransactionTestCase):
    """Test the provider genre backfill command without external requests."""

    reset_sequences = True

    def setUp(self):
        """Create provider-backed items in a known Item ID order."""
        self.items = [
            Item.objects.create(
                media_id=f"movie-{index}",
                source=Sources.TMDB.value,
                media_type=MediaTypes.MOVIE.value,
                title=f"Movie {index}",
                image="https://example.com/movie.jpg",
            )
            for index in range(2)
        ]

    @patch(f"{COMMAND_PATH}.services.get_media_metadata")
    def test_backfill_continues_after_provider_failure(self, mock_metadata):
        """One failed provider response does not stop later accounting."""
        mock_metadata.side_effect = [
            {"genres": ["Crime", "crime"]},
            RuntimeError("provider unavailable"),
        ]
        stdout = StringIO()
        stderr = StringIO()

        call_command(
            "backfill_provider_genres",
            batch_size=1,
            delay=0,
            stdout=stdout,
            stderr=stderr,
        )

        self.assertEqual(ProviderGenre.objects.count(), 1)
        self.assertIn("successes=1, skips=0, failures=1", stdout.getvalue())
        self.assertIn("provider requests=2", stdout.getvalue())
        self.assertIn(": failed (provider unavailable)", stderr.getvalue())
        self.assertEqual(mock_metadata.call_count, 2)

    @patch(f"{COMMAND_PATH}.services.get_media_metadata")
    def test_backfill_dry_run_and_limit_do_not_write(self, mock_metadata):
        """Dry-run honors the limit and leaves genre tables unchanged."""
        mock_metadata.return_value = {"genres": ["Crime"]}
        stdout = StringIO()

        call_command(
            "backfill_provider_genres",
            dry_run=True,
            limit=1,
            batch_size=1,
            delay=0,
            stdout=stdout,
        )

        self.assertFalse(ProviderGenre.objects.exists())
        self.assertEqual(mock_metadata.call_count, 1)
        self.assertIn("DRY RUN: successes=1, skips=0, failures=0", stdout.getvalue())
        self.assertFalse(
            mock_metadata.call_args.kwargs["sync_provider_genres"],
        )

    @patch(f"{COMMAND_PATH}.services.get_media_metadata")
    def test_populated_items_are_skipped_by_default(self, mock_metadata):
        """Existing provider genres are reusable progress by default."""
        crime = ProviderGenre.objects.create(name="Crime")
        self.items[0].provider_genres.add(crime)
        mock_metadata.return_value = {"genres": ["Mystery"]}
        stdout = StringIO()

        call_command(
            "backfill_provider_genres",
            delay=0,
            stdout=stdout,
        )

        self.assertEqual(mock_metadata.call_count, 1)
        self.assertEqual(
            mock_metadata.call_args.args[1],
            self.items[1].media_id,
        )
        self.assertIn(
            f"Item {self.items[0].id} tmdb/movie/movie-0: "
            "skipped (provider genres already stored)",
            stdout.getvalue(),
        )
        self.assertIn("successes=1, skips=1, failures=0", stdout.getvalue())

    @patch(f"{COMMAND_PATH}.services.get_media_metadata")
    def test_force_refresh_processes_populated_items(self, mock_metadata):
        """Force-refresh explicitly replaces already populated Items."""
        crime = ProviderGenre.objects.create(name="Crime")
        self.items[0].provider_genres.add(crime)
        mock_metadata.return_value = {"genres": ["Mystery"]}

        call_command(
            "backfill_provider_genres",
            force_refresh=True,
            delay=0,
            stdout=StringIO(),
        )

        self.assertEqual(mock_metadata.call_count, 2)
        self.items[0].refresh_from_db()
        self.assertEqual(
            list(
                self.items[0].provider_genres.values_list(
                    "normalized_name",
                    flat=True,
                ),
            ),
            ["mystery"],
        )

    @patch(f"{COMMAND_PATH}.services.get_media_metadata")
    def test_start_after_uses_stable_item_id_order(self, mock_metadata):
        """Start-after resumes strictly after an Item ID in stable order."""
        third_item = Item.objects.create(
            media_id="movie-2",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Movie 2",
            image="https://example.com/movie.jpg",
        )
        mock_metadata.return_value = {"genres": ["Crime"]}
        stdout = StringIO()

        call_command(
            "backfill_provider_genres",
            start_after=self.items[0].id,
            batch_size=1,
            delay=0,
            stdout=stdout,
        )

        requested_media_ids = [
            metadata_call.args[1] for metadata_call in mock_metadata.call_args_list
        ]
        self.assertEqual(
            requested_media_ids,
            [self.items[1].media_id, third_item.media_id],
        )
        self.assertIn(f"[1/2] Item {self.items[1].id}", stdout.getvalue())
        self.assertIn(f"[2/2] Item {third_item.id}", stdout.getvalue())

    @patch(f"{COMMAND_PATH}.services.get_media_metadata")
    def test_limit_caps_selected_items(self, mock_metadata):
        """Limit applies after stable ordering and resume filtering."""
        mock_metadata.return_value = {"genres": ["Crime"]}

        call_command(
            "backfill_provider_genres",
            limit=1,
            batch_size=1,
            delay=0,
            stdout=StringIO(),
        )

        self.assertEqual(mock_metadata.call_count, 1)
        self.assertEqual(mock_metadata.call_args.args[1], self.items[0].media_id)

    @patch(f"{COMMAND_PATH}.time.sleep")
    @patch(f"{COMMAND_PATH}.services.get_media_metadata")
    def test_delay_only_occurs_between_real_provider_requests(
        self,
        mock_metadata,
        mock_sleep,
    ):
        """Skipped rows do not add sleeps; subsequent requests are throttled."""
        skipped_genre = ProviderGenre.objects.create(name="Existing")
        self.items[1].provider_genres.add(skipped_genre)
        third_item = Item.objects.create(
            media_id="movie-2",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Movie 2",
            image="https://example.com/movie.jpg",
        )
        mock_metadata.return_value = {"genres": ["Crime"]}

        call_command(
            "backfill_provider_genres",
            batch_size=1,
            delay=1.5,
            stdout=StringIO(),
        )

        requested_media_ids = [
            metadata_call.args[1] for metadata_call in mock_metadata.call_args_list
        ]
        self.assertEqual(
            requested_media_ids,
            [self.items[0].media_id, third_item.media_id],
        )
        mock_sleep.assert_called_once_with(1.5)

    @patch(f"{COMMAND_PATH}.time.sleep")
    @patch(f"{COMMAND_PATH}.services.get_media_metadata")
    @patch(f"{COMMAND_PATH}.synchronize_provider_genres")
    def test_database_lock_retries_then_succeeds(
        self,
        mock_synchronize,
        mock_metadata,
        mock_sleep,
    ):
        """SQLite lock retries use exponential backoff and can recover."""
        self.items[1].delete()
        mock_metadata.return_value = {"genres": ["Crime"]}
        attempts = 0

        def lock_then_synchronize(*args):
            nonlocal attempts
            attempts += 1
            if attempts < SUCCESSFUL_LOCK_ATTEMPT:
                lock_error = "database is locked"
                raise OperationalError(lock_error)
            return synchronize_provider_genres(*args)

        mock_synchronize.side_effect = lock_then_synchronize
        stdout = StringIO()

        call_command(
            "backfill_provider_genres",
            delay=0,
            stdout=stdout,
            stderr=StringIO(),
        )

        self.assertEqual(mock_synchronize.call_count, 3)
        self.assertEqual(mock_sleep.call_args_list, [call(1), call(2)])
        self.assertIn("database lock retries=2", stdout.getvalue())
        self.assertIn("successes=1, skips=0, failures=0", stdout.getvalue())
        self.assertTrue(self.items[0].provider_genres.exists())

    @patch(f"{COMMAND_PATH}.time.sleep")
    @patch(f"{COMMAND_PATH}.services.get_media_metadata")
    @patch(f"{COMMAND_PATH}.synchronize_provider_genres")
    def test_lock_retry_exhaustion_reports_failure_and_continues(
        self,
        mock_synchronize,
        mock_metadata,
        mock_sleep,
    ):
        """Exhausted lock retries fail one Item without stopping the run."""
        mock_metadata.return_value = {"genres": ["Crime"]}

        def fail_first_item(media_type, media_id, source, metadata):
            if media_id == self.items[0].media_id:
                lock_error = "DATABASE IS LOCKED"
                raise OperationalError(lock_error)
            return synchronize_provider_genres(
                media_type,
                media_id,
                source,
                metadata,
            )

        mock_synchronize.side_effect = fail_first_item
        stdout = StringIO()
        stderr = StringIO()

        call_command(
            "backfill_provider_genres",
            delay=0,
            stdout=stdout,
            stderr=stderr,
        )

        self.assertEqual(mock_metadata.call_count, 2)
        self.assertEqual(mock_sleep.call_args_list, [call(1), call(2), call(4)])
        self.assertIn("successes=1, skips=0, failures=1", stdout.getvalue())
        self.assertIn("database lock retries=3", stdout.getvalue())
        self.assertIn(f"Item {self.items[0].id}", stderr.getvalue())
        self.assertTrue(self.items[1].provider_genres.exists())

    @patch(f"{COMMAND_PATH}.time.sleep")
    @patch(f"{COMMAND_PATH}.services.get_media_metadata")
    @patch(f"{COMMAND_PATH}.synchronize_provider_genres")
    def test_unrelated_operational_error_is_not_retried(
        self,
        mock_synchronize,
        mock_metadata,
        mock_sleep,
    ):
        """Only the exact SQLite lock failure receives retry handling."""
        self.items[1].delete()
        mock_metadata.return_value = {"genres": ["Crime"]}
        mock_synchronize.side_effect = OperationalError("disk I/O error")

        call_command(
            "backfill_provider_genres",
            delay=0,
            stdout=StringIO(),
            stderr=StringIO(),
        )

        self.assertEqual(mock_synchronize.call_count, 1)
        mock_sleep.assert_not_called()

    @patch(f"{COMMAND_PATH}.services.get_media_metadata")
    def test_ctrl_c_prints_summary_and_resume_command(self, mock_metadata):
        """Keyboard interruption preserves completed rows and gives a resume point."""
        mock_metadata.side_effect = [
            {"genres": ["Crime"]},
            KeyboardInterrupt,
        ]
        stdout = StringIO()
        stderr = StringIO()

        call_command(
            "backfill_provider_genres",
            limit=2,
            batch_size=1,
            delay=0,
            stdout=stdout,
            stderr=stderr,
        )

        output = stdout.getvalue()
        self.assertIn("INTERRUPTED: successes=1, skips=0, failures=0", output)
        self.assertIn("provider requests=2", output)
        self.assertIn(
            f"last completed Item ID={self.items[0].id}",
            output,
        )
        self.assertIn(f"Last attempted Item ID: {self.items[1].id}", output)
        self.assertIn(
            "Resume command: python manage.py backfill_provider_genres "
            f"--start-after {self.items[0].id} --batch-size 1 "
            "--delay 0 --limit 1",
            output,
        )
        self.assertIn("completed database writes were preserved", stderr.getvalue())
        self.assertTrue(self.items[0].provider_genres.exists())
        self.assertFalse(self.items[1].provider_genres.exists())

    @patch(f"{COMMAND_PATH}.services.get_media_metadata")
    def test_provider_fetch_occurs_outside_database_transaction(self, mock_metadata):
        """The command never holds an atomic write transaction during fetching."""
        transaction_states = []

        def metadata_response(*_args, **_kwargs):
            transaction_states.append(connection.in_atomic_block)
            return {"genres": ["Crime"]}

        mock_metadata.side_effect = metadata_response

        call_command(
            "backfill_provider_genres",
            delay=0,
            stdout=StringIO(),
        )

        self.assertEqual(transaction_states, [False, False])

    @patch(f"{COMMAND_PATH}.services.get_media_metadata")
    def test_tv_family_is_fetched_once_and_synchronized_together(
        self,
        mock_metadata,
    ):
        """One TV request populates the parent and all stored seasons."""
        Item.objects.all().delete()
        tv_item = Item.objects.create(
            media_id="tv-family",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="TV Family",
            image="https://example.com/tv.jpg",
        )
        season_items = [
            Item.objects.create(
                media_id="tv-family",
                source=Sources.TMDB.value,
                media_type=MediaTypes.SEASON.value,
                season_number=season_number,
                title="TV Family",
                image="https://example.com/season.jpg",
            )
            for season_number in (1, 2)
        ]
        mock_metadata.return_value = {"genres": ["Crime"]}

        call_command(
            "backfill_provider_genres",
            batch_size=3,
            delay=0,
            stdout=StringIO(),
        )

        self.assertEqual(mock_metadata.call_count, 1)
        self.assertEqual(mock_metadata.call_args.args[0], MediaTypes.TV.value)
        for item in [tv_item, *season_items]:
            self.assertEqual(
                list(
                    item.provider_genres.values_list(
                        "normalized_name",
                        flat=True,
                    ),
                ),
                ["crime"],
            )
