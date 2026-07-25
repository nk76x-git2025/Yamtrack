from unittest.mock import patch

from django.test import TestCase

from app.models import Item, MediaTypes, ProviderGenre, Sources
from app.providers import services


class ProviderGenreSynchronizationTests(TestCase):
    """Test synchronization from successful provider metadata responses."""

    def _create_item(self, media_type, *, season_number=None, episode_number=None):
        return Item.objects.create(
            media_id="show-1",
            source=Sources.TMDB.value,
            media_type=media_type,
            title="Test Show",
            image="https://example.com/show.jpg",
            season_number=season_number,
            episode_number=episode_number,
        )

    @patch("app.providers.tmdb.tv")
    def test_successful_metadata_fetch_synchronizes_tv_and_seasons(self, mock_tv):
        """TV metadata is copied to the parent and stored season Items."""
        tv_item = self._create_item(MediaTypes.TV.value)
        season_one = self._create_item(MediaTypes.SEASON.value, season_number=1)
        season_two = self._create_item(MediaTypes.SEASON.value, season_number=2)
        episode = self._create_item(
            MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=1,
        )
        mock_tv.return_value = {
            "title": "Test Show",
            "genres": ["Crime", " Mystery ", "crime"],
        }

        services.get_media_metadata(
            MediaTypes.TV.value,
            tv_item.media_id,
            tv_item.source,
        )

        for item in [tv_item, season_one, season_two]:
            self.assertEqual(
                set(item.provider_genres.values_list("normalized_name", flat=True)),
                {"crime", "mystery"},
            )
        self.assertFalse(episode.provider_genres.exists())
        self.assertEqual(ProviderGenre.objects.count(), 2)

    @patch("app.providers.tmdb.movie")
    def test_failed_provider_fetch_preserves_stored_genres(self, mock_movie):
        """A provider exception cannot delete previously stored genres."""
        movie = Item.objects.create(
            media_id="movie-1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Movie",
            image="https://example.com/movie.jpg",
        )
        crime = ProviderGenre.objects.create(name="Crime")
        movie.provider_genres.add(crime)
        mock_movie.side_effect = RuntimeError("provider unavailable")

        with self.assertRaises(RuntimeError):
            services.get_media_metadata(
                MediaTypes.MOVIE.value,
                movie.media_id,
                movie.source,
            )

        self.assertEqual(
            list(movie.provider_genres.values_list("normalized_name", flat=True)),
            ["crime"],
        )

    @patch("app.providers.tmdb.movie")
    def test_metadata_without_genre_field_preserves_stored_genres(self, mock_movie):
        """A response without genre data does not look like an empty genre result."""
        movie = Item.objects.create(
            media_id="movie-2",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Movie",
            image="https://example.com/movie.jpg",
        )
        crime = ProviderGenre.objects.create(name="Crime")
        movie.provider_genres.add(crime)
        mock_movie.return_value = {"title": "Movie"}

        services.get_media_metadata(
            MediaTypes.MOVIE.value,
            movie.media_id,
            movie.source,
        )

        self.assertEqual(
            list(movie.provider_genres.values_list("normalized_name", flat=True)),
            ["crime"],
        )
