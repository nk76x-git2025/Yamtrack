import json
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from app.models import (
    Anime,
    Book,
    BookProgressUnits,
    Item,
    MediaTypes,
    Movie,
    ProviderGenre,
    ProviderGenreMatchMode,
    Sources,
    Status,
    Tag,
    TaggedMedia,
)
from app.templatetags import app_tags
from users.forms import UserUpdateForm
from users.models import MediaRatingChoices


class MediaListViewTests(TestCase):
    """Test the media list view."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.external_credentials = {
            "username": "test2",
            "password": "12345",
            "profile_private": True,
        }
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.external_user = get_user_model().objects.create_user(
            **self.external_credentials
        )
        self.metadata_patcher = patch("app.providers.services.get_media_metadata")
        self.mock_get_media_metadata = self.metadata_patcher.start()
        self.mock_get_media_metadata.return_value = {"max_progress": 1}
        self.addCleanup(self.metadata_patcher.stop)
        self.client.login(**self.credentials)

        movies_id = ["278", "238", "129", "424", "680"]
        num_completed = 3
        for i in range(1, 6):
            item = Item.objects.create(
                media_id=movies_id[i - 1],
                source=Sources.TMDB.value,
                media_type=MediaTypes.MOVIE.value,
                title=f"Test Movie {i}",
                image="http://example.com/image.jpg",
            )
            status = (
                Status.COMPLETED.value
                if i < num_completed
                else Status.IN_PROGRESS.value
            )
            Movie.objects.create(
                item=item,
                user=self.user,
                status=status,
                progress=1 if i < num_completed else 0,
                score=i,
            )

    def test_media_list_view(self):
        """Test the media list view displays media items."""
        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value])
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/media_list.html")

        self.assertIn("media_list", response.context)
        self.assertEqual(response.context["media_list"].paginator.count, 5)
        self.assertEqual(response.context["filtered_count"], 5)
        self.assertEqual(response.context["result_count_text"], "5 items")
        self.assertContains(response, 'id="media-result-count"', count=1)
        self.assertContains(response, "5 items")
        self.assertNotContains(response, "Showing")

        self.assertIn("sort_choices", response.context)
        self.assertIn("status_choices", response.context)
        self.assertEqual(response.context["media_type"], MediaTypes.MOVIE.value)
        self.assertEqual(
            response.context["media_type_plural"],
            app_tags.media_type_readable_plural(MediaTypes.MOVIE.value).lower(),
        )

    def test_media_list_with_filters(self):
        """Test the media list view with filters."""
        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value])
            + "?status=Completed&sort=score&sort_direction=asc&layout=table",
        )

        self.assertEqual(response.status_code, 200)

        self.assertEqual(
            response.context["current_status"],
            Status.COMPLETED.value,
        )
        self.assertEqual(response.context["current_sort"], "score")
        self.assertEqual(response.context["current_sort_direction"], "asc")
        self.assertEqual(response.context["current_layout"], "table")

        self.assertEqual(response.context["media_list"].paginator.count, 2)
        self.assertEqual(response.context["filtered_count"], 2)
        self.assertEqual(response.context["result_count_text"], "2 matching items")
        self.assertContains(response, "2 matching items")

        self.user.refresh_from_db()
        self.assertEqual(self.user.movie_status, Status.COMPLETED.value)
        self.assertEqual(self.user.movie_sort, "score")
        self.assertEqual(self.user.movie_layout, "table")

    def test_media_list_with_rating_filter(self):
        """Test rating filter is applied and exposed in the media list view."""
        unrated_item = Item.objects.create(
            media_id="unrated",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Unrated Movie",
            image="http://example.com/image.jpg",
        )
        Movie.objects.create(
            item=unrated_item,
            user=self.user,
            status=Status.COMPLETED.value,
            score=None,
        )

        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value])
            + "?rating_filter=unrated&search=Unrated"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["current_rating_filter"],
            MediaRatingChoices.UNRATED,
        )
        self.assertIn("rating_filter_choices", response.context)
        self.assertEqual(response.context["media_list"].paginator.count, 1)
        self.assertEqual(response.context["result_count_text"], "1 matching item")
        self.assertContains(response, "1 matching item")
        self.assertIsNone(response.context["media_list"].object_list[0].score)

    def test_media_list_rating_filter_buckets_decimals(self):
        """Test decimal rating buckets include only the matching rating range."""
        ratings = [7.9, 8, 8.1, 8.5, 8.9, 9, 10]
        for index, rating in enumerate(ratings):
            item = Item.objects.create(
                media_id=f"bucket-{index}",
                source=Sources.TMDB.value,
                media_type=MediaTypes.MOVIE.value,
                title=f"Bucket {rating}",
                image="http://example.com/image.jpg",
            )
            Movie.objects.create(
                item=item,
                user=self.user,
                status=Status.COMPLETED.value,
                score=rating,
            )

        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value])
            + "?rating_filter=8&sort=score&sort_direction=asc"
        )

        self.assertEqual(response.status_code, 200)
        scores = [
            media.score
            for media in response.context["media_list"].object_list
        ]
        self.assertEqual(
            scores,
            [Decimal("8.0"), Decimal("8.1"), Decimal("8.5"), Decimal("8.9")],
        )

    def test_media_list_rating_filter_works_with_tags(self):
        """Test rating filter combines with tag filters."""
        comedy = Tag.objects.create(
            user=self.user,
            name="Comedy",
            normalized_name="comedy",
        )
        unrated_item = Item.objects.create(
            media_id="tag-unrated",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Tagged Unrated",
            image="http://example.com/image.jpg",
        )
        unrated_movie = Movie.objects.create(
            item=unrated_item,
            user=self.user,
            status=Status.COMPLETED.value,
            score=None,
        )
        TaggedMedia.objects.create(
            user=self.user,
            tag=comedy,
            content_object=unrated_movie,
        )

        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value])
            + "?rating_filter=unrated&tags=Comedy"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["media_list"].paginator.count, 1)
        self.assertEqual(response.context["selected_tags"], ["Comedy"])

    def test_media_list_sort_direction_applies(self):
        """Test sort direction can reverse title ordering."""
        item_a = Item.objects.create(
            media_id="9991",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="AAA Movie",
            image="http://example.com/image.jpg",
        )
        Movie.objects.create(
            item=item_a,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=0,
            score=2,
        )
        item_z = Item.objects.create(
            media_id="9992",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="ZZZ Movie",
            image="http://example.com/image.jpg",
        )
        Movie.objects.create(
            item=item_z,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=0,
            score=2,
        )

        asc_response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value])
            + "?sort=title&sort_direction=asc"
        )
        desc_response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value])
            + "?sort=title&sort_direction=desc"
        )

        asc_titles = [
            media.item.title
            for media in asc_response.context["media_list"].object_list
        ]
        desc_titles = [
            media.item.title
            for media in desc_response.context["media_list"].object_list
        ]
        self.assertLess(asc_titles.index("AAA Movie"), asc_titles.index("ZZZ Movie"))
        self.assertLess(desc_titles.index("ZZZ Movie"), desc_titles.index("AAA Movie"))

    def test_media_list_htmx_request(self):
        """Test the media list view with HTMX request."""
        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value])
            + "?layout=grid",
            headers={"hx-request": "true"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/media_grid_items.html")
        self.assertNotContains(response, 'id="media-result-count"')
        self.assertNotContains(response, 'hx-swap-oob="true"')
        self.assertEqual(
            json.loads(response.headers["HX-Trigger"]),
            {"media-result-count-updated": {"text": "5 items"}},
        )

        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value])
            + "?layout=table",
            headers={"hx-request": "true"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/media_table_items.html")
        self.assertNotContains(response, 'id="media-result-count"')
        self.assertEqual(
            json.loads(response.headers["HX-Trigger"]),
            {"media-result-count-updated": {"text": "5 items"}},
        )

    def test_media_list_count_uses_total_count_for_infinite_scroll(self):
        """Test count text uses the total filtered count, not page range."""
        for i in range(6, 36):
            item = Item.objects.create(
                media_id=f"page-{i}",
                source=Sources.TMDB.value,
                media_type=MediaTypes.MOVIE.value,
                title=f"Paged Movie {i}",
                image="http://example.com/image.jpg",
            )
            Movie.objects.create(
                item=item,
                user=self.user,
                status=Status.COMPLETED.value,
                progress=1,
                score=1,
            )

        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value])
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["filtered_count"], 35)
        self.assertEqual(response.context["result_count_text"], "35 items")
        self.assertContains(response, "35 items")
        self.assertNotContains(response, "Showing")

        page_two_response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value])
            + "?page=2"
        )
        self.assertEqual(page_two_response.status_code, 200)
        self.assertEqual(page_two_response.context["filtered_count"], 35)
        self.assertEqual(page_two_response.context["result_count_text"], "35 items")
        self.assertContains(page_two_response, "35 items")
        self.assertNotContains(page_two_response, "Showing")

    def test_media_list_zero_result_count(self):
        """Test zero-result filters show a clean count."""
        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value])
            + "?search=definitely-no-matching-title"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["filtered_count"], 0)
        self.assertEqual(response.context["result_count_text"], "No matching items")
        self.assertContains(response, "No matching items")

    def test_book_list_displays_selected_progress_unit(self):
        """Book category pages display the selected progress unit."""
        item = Item.objects.create(
            media_id="book-1",
            source=Sources.HARDCOVER.value,
            media_type=MediaTypes.BOOK.value,
            title="Book One",
            image="http://example.com/book.jpg",
        )
        Book.objects.create(
            item=item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=7,
            progress_unit=BookProgressUnits.CHAPTERS.value,
        )

        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.BOOK.value])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "7 Chapters")
        self.assertNotContains(response, "7 / 1 Chapters")

    def test_media_list_filter_by_single_tag_and_clear(self):
        """Test filtering by a tag and clearing the filter."""
        comedy = Tag.objects.create(
            user=self.user,
            name="Comedy",
            normalized_name="comedy",
        )
        TaggedMedia.objects.create(
            user=self.user,
            tag=comedy,
            content_object=Movie.objects.first(),
        )

        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value])
            + "?tags=Comedy"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["media_list"].paginator.count, 1)
        self.assertEqual(response.context["selected_tags"], ["Comedy"])

        clear_response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value])
        )
        self.assertEqual(clear_response.status_code, 200)
        self.assertEqual(clear_response.context["media_list"].paginator.count, 5)

    def test_media_list_tag_filter_combines_with_search_and_status(self):
        """Test tag filter combines with search/status filters."""
        comedy = Tag.objects.create(
            user=self.user,
            name="Comedy",
            normalized_name="comedy",
        )
        movie = Movie.objects.filter(status=Status.COMPLETED.value).first()
        TaggedMedia.objects.create(user=self.user, tag=comedy, content_object=movie)

        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value])
            + f"?tags=Comedy&status={Status.COMPLETED.value}&search={movie.item.title}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["media_list"].paginator.count, 1)

    def test_media_list_filters_by_single_provider_genre(self):
        """One genre excludes tracked items without matching genre metadata."""
        crime = ProviderGenre.objects.create(name="Crime")
        matching_movie = Movie.objects.first()
        matching_movie.item.provider_genres.add(crime)

        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value])
            + "?genres=Crime"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_genres"], ["crime"])
        self.assertEqual(response.context["media_list"].paginator.count, 1)
        self.assertEqual(
            response.context["media_list"].object_list[0].item,
            matching_movie.item,
        )

    def test_media_list_multiple_provider_genres_use_or(self):
        """Missing genre mode defaults to Crime OR Mystery."""
        crime = ProviderGenre.objects.create(name="Crime")
        mystery = ProviderGenre.objects.create(name="Mystery")
        movies = list(Movie.objects.order_by("id")[:3])
        movies[0].item.provider_genres.add(crime)
        movies[1].item.provider_genres.add(mystery)
        movies[2].item.provider_genres.add(crime, mystery)

        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value])
            + "?genres=crime&genres=mystery"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["media_list"].paginator.count, 3)
        self.assertEqual(response.context["selected_genres"], ["crime", "mystery"])
        self.assertEqual(
            response.context["current_genre_mode"],
            ProviderGenreMatchMode.ANY,
        )

    def test_media_list_explicit_match_any_allows_either_genre(self):
        """Explicit match-any mode includes items with either selected genre."""
        crime = ProviderGenre.objects.create(name="Crime")
        mystery = ProviderGenre.objects.create(name="Mystery")
        movies = list(Movie.objects.order_by("id")[:3])
        movies[0].item.provider_genres.add(crime)
        movies[1].item.provider_genres.add(mystery)
        movies[2].item.provider_genres.add(crime, mystery)

        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value])
            + "?genres=crime&genres=mystery&genre_mode=any"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["media_list"].paginator.count, 3)
        self.assertEqual(
            response.context["current_genre_mode"],
            ProviderGenreMatchMode.ANY,
        )

    def test_media_list_match_all_requires_every_selected_genre(self):
        """Match-all mode only includes items with every selected genre."""
        crime = ProviderGenre.objects.create(name="Crime")
        mystery = ProviderGenre.objects.create(name="Mystery")
        movies = list(Movie.objects.order_by("id")[:3])
        movies[0].item.provider_genres.add(crime)
        movies[1].item.provider_genres.add(mystery)
        movies[2].item.provider_genres.add(crime, mystery)

        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value])
            + "?genres=crime&genres=mystery&genre_mode=all"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["media_list"].paginator.count, 1)
        self.assertEqual(
            response.context["media_list"].object_list[0].item,
            movies[2].item,
        )
        self.assertEqual(
            response.context["current_genre_mode"],
            ProviderGenreMatchMode.ALL,
        )

    def test_media_list_invalid_genre_mode_falls_back_to_any(self):
        """Invalid genre modes preserve the default OR behavior."""
        crime = ProviderGenre.objects.create(name="Crime")
        mystery = ProviderGenre.objects.create(name="Mystery")
        movies = list(Movie.objects.order_by("id")[:2])
        movies[0].item.provider_genres.add(crime)
        movies[1].item.provider_genres.add(mystery)

        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value])
            + "?genres=crime&genres=mystery&genre_mode=unsupported"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["media_list"].paginator.count, 2)
        self.assertEqual(
            response.context["current_genre_mode"],
            ProviderGenreMatchMode.ANY,
        )

    def test_media_list_genre_modes_match_with_zero_or_one_selection(self):
        """Any and all modes are equivalent for fewer than two genres."""
        crime = ProviderGenre.objects.create(name="Crime")
        Movie.objects.first().item.provider_genres.add(crime)

        for genres, expected_count in (("", 5), ("&genres=crime", 1)):
            counts = []
            for mode in ProviderGenreMatchMode.values:
                response = self.client.get(
                    reverse(
                        "medialist",
                        args=[self.user.username, MediaTypes.MOVIE.value],
                    )
                    + f"?genre_mode={mode}{genres}"
                )
                counts.append(response.context["media_list"].paginator.count)

            with self.subTest(genres=genres):
                self.assertEqual(counts, [expected_count, expected_count])

    def test_media_list_provider_genre_combines_with_status(self):
        """Genre filtering combines with status using AND behavior."""
        crime = ProviderGenre.objects.create(name="Crime")
        completed = Movie.objects.filter(status=Status.COMPLETED.value).first()
        in_progress = Movie.objects.filter(status=Status.IN_PROGRESS.value).first()
        completed.item.provider_genres.add(crime)
        in_progress.item.provider_genres.add(crime)

        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value])
            + f"?genres=crime&status={Status.COMPLETED.value}"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["media_list"].paginator.count, 1)
        self.assertEqual(
            response.context["media_list"].object_list[0].status,
            Status.COMPLETED.value,
        )

    def test_match_all_combines_with_status_and_media_type(self):
        """Match-all remains ANDed with the status and media category filters."""
        crime = ProviderGenre.objects.create(name="Crime")
        mystery = ProviderGenre.objects.create(name="Mystery")
        completed_movie = Movie.objects.filter(
            status=Status.COMPLETED.value
        ).first()
        in_progress_movie = Movie.objects.filter(
            status=Status.IN_PROGRESS.value
        ).first()
        completed_movie.item.provider_genres.add(crime, mystery)
        in_progress_movie.item.provider_genres.add(crime, mystery)

        anime_item = Item.objects.create(
            media_id="matching-anime",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Matching Anime",
            image="https://example.com/anime.jpg",
        )
        anime_item.provider_genres.add(crime, mystery)
        Anime.objects.create(
            item=anime_item,
            user=self.user,
            status=Status.COMPLETED.value,
        )

        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value])
            + (
                f"?status={Status.COMPLETED.value}"
                "&genres=crime&genres=mystery&genre_mode=all"
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["media_list"].paginator.count, 1)
        self.assertEqual(
            response.context["media_list"].object_list[0].item,
            completed_movie.item,
        )

    def test_provider_genres_are_limited_by_user_and_media_type(self):
        """Genre choices only use the target user's current media category."""
        crime = ProviderGenre.objects.create(name="Crime")
        anime_only = ProviderGenre.objects.create(name="Anime Only")
        other_user_only = ProviderGenre.objects.create(name="Other User Only")
        movie = Movie.objects.first()
        movie.item.provider_genres.add(crime)

        anime_item = Item.objects.create(
            media_id="anime-genre",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Genre Anime",
            image="https://example.com/anime.jpg",
        )
        anime_item.provider_genres.add(crime, anime_only)
        Anime.objects.create(item=anime_item, user=self.user)

        external_item = Item.objects.create(
            media_id="external-genre",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="External Movie",
            image="https://example.com/external.jpg",
        )
        external_item.provider_genres.add(other_user_only)
        Movie.objects.create(item=external_item, user=self.external_user)

        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value])
        )

        available = {
            genre.normalized_name for genre in response.context["available_genres"]
        }
        self.assertEqual(available, {"crime"})

        filtered_response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value])
            + "?genres=crime"
        )
        self.assertEqual(
            filtered_response.context["media_list"].paginator.count,
            1,
        )

    def test_genre_query_parameters_persist_without_provider_calls(self):
        """Genre selections are included in shared filter and layout state."""
        crime = ProviderGenre.objects.create(name="Crime")
        mystery = ProviderGenre.objects.create(name="Mystery")
        movie = Movie.objects.first()
        movie.item.provider_genres.add(crime, mystery)
        self.mock_get_media_metadata.reset_mock()

        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value])
            + (
                "?genres=Crime&genres=mystery&genre_mode=all"
                "&sort=title&layout=grid"
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_genres"], ["crime", "mystery"])
        self.assertEqual(
            response.context["current_genre_mode"],
            ProviderGenreMatchMode.ALL,
        )
        self.assertContains(response, 'name="genres"')
        self.assertContains(response, 'name="genre_mode"')
        self.assertContains(response, "params.append('genres', genre)")
        self.assertContains(response, "params.set('genre_mode', this.genreMode)")
        self.assertContains(response, "`${genres.length} selected`")
        self.assertContains(response, "Genre matching:")
        self.assertContains(response, "Match any")
        self.assertContains(response, "Match all")
        self.assertContains(response, 'hx-push-url="true"')
        self.mock_get_media_metadata.assert_not_called()

    def test_media_list_count_renders_for_all_category_pages(self):
        """Test category pages render the empty count without server errors."""
        category_media_types = [
            MediaTypes.TV.value,
            MediaTypes.SEASON.value,
            MediaTypes.MOVIE.value,
            MediaTypes.ANIME.value,
            MediaTypes.MANGA.value,
            MediaTypes.GAME.value,
            MediaTypes.BOOK.value,
            MediaTypes.COMIC.value,
            MediaTypes.BOARDGAME.value,
            MediaTypes.EXPERIENCE.value,
        ]

        for media_type in category_media_types:
            with self.subTest(media_type=media_type):
                response = self.client.get(
                    reverse("medialist", args=[self.user.username, media_type])
                )

                self.assertEqual(response.status_code, 200)
                self.assertIn("filtered_count", response.context)
                self.assertContains(response, "items")
                self.assertNotContains(response, "Showing")

    def test_public_media_list_ignores_invalid_filters(self):
        """Test invalid public filters fall back to the target user's preferences."""
        self.external_user.profile_private = False
        self.external_user.save(update_fields=["profile_private"])

        response = self.client.get(
            reverse(
                "medialist", args=[self.external_user.username, MediaTypes.MOVIE.value]
            )
            + "?status=invalid&sort=bad_field&layout=invalid",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["current_status"], self.external_user.movie_status
        )
        self.assertEqual(
            response.context["current_sort"], self.external_user.movie_sort
        )
        self.assertEqual(
            response.context["current_layout"], self.external_user.movie_layout
        )

    def test_anonymous_user_can_view_public_media_list(self):
        """Test anonymous users can view public media lists."""
        self.external_user.profile_private = False
        self.external_user.save(update_fields=["profile_private"])
        self.client.logout()

        response = self.client.get(
            reverse(
                "medialist", args=[self.external_user.username, MediaTypes.MOVIE.value]
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("media_list", response.context)

    def test_profile_private_defaults_to_true(self):
        """Test new users have private profiles by default."""
        user = get_user_model().objects.create_user(
            username="private-default",
        )

        self.assertTrue(user.profile_private)

    def test_private_media_list(self):
        """Test the private media list view."""
        response = self.client.get(
            reverse(
                "medialist", args=[self.external_user.username, MediaTypes.MOVIE.value]
            )
        )
        self.assertEqual(response.status_code, 404)

        form = UserUpdateForm(
            data={"username": "test2", "profile_private": False},
            instance=self.external_user,
        )
        self.assertTrue(form.is_valid(), form.errors)
        external_user = form.save()
        external_user.refresh_from_db()

        response = self.client.get(
            reverse(
                "medialist", args=[self.external_user.username, MediaTypes.MOVIE.value]
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("media_list", response.context)
