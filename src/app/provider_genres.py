import logging
from dataclasses import dataclass

from django.db import transaction

from app.models import (
    Item,
    MediaTypes,
    ProviderGenre,
    Sources,
    normalize_provider_genre_name,
)

logger = logging.getLogger(__name__)

TV_METADATA_TYPES = {
    MediaTypes.TV.value,
    MediaTypes.SEASON.value,
    "tv_with_seasons",
}
UNSUPPORTED_MEDIA_TYPES = {
    MediaTypes.EPISODE.value,
    MediaTypes.EXPERIENCE.value,
}


@dataclass(frozen=True)
class ProviderGenreSyncResult:
    """Summary of a provider genre synchronization."""

    genre_count: int
    item_count: int


def genres_from_metadata(metadata):
    """Return deduplicated display/normalized genre pairs from metadata."""
    if not isinstance(metadata, dict) or "genres" not in metadata:
        return None

    raw_genres = metadata.get("genres")
    if raw_genres is None or not isinstance(raw_genres, (list, tuple, set)):
        return None

    genres = []
    seen = set()
    for raw_name in raw_genres:
        display_name = " ".join(str(raw_name or "").split())[:100]
        normalized_name = normalize_provider_genre_name(display_name)
        if not normalized_name or normalized_name in seen:
            continue
        seen.add(normalized_name)
        genres.append((display_name, normalized_name))
    return genres


def _target_items(media_type, media_id, source):
    """Return items that should receive genres for provider metadata."""
    if source == Sources.MANUAL.value or media_type in UNSUPPORTED_MEDIA_TYPES:
        return Item.objects.none()

    filters = {
        "media_id": str(media_id),
        "source": source,
    }
    if media_type in TV_METADATA_TYPES:
        filters["media_type__in"] = [
            MediaTypes.TV.value,
            MediaTypes.SEASON.value,
        ]
    else:
        filters["media_type"] = media_type
    return Item.objects.filter(**filters)


def synchronize_provider_genres(media_type, media_id, source, metadata):
    """Replace stored provider genres after a successful metadata response."""
    genres = genres_from_metadata(metadata)
    if genres is None:
        return None

    target_items = list(_target_items(media_type, media_id, source))
    if not target_items:
        return ProviderGenreSyncResult(
            genre_count=len(genres),
            item_count=0,
        )

    with transaction.atomic():
        normalized_names = [normalized_name for _, normalized_name in genres]
        existing_names = set(
            ProviderGenre.objects.filter(
                normalized_name__in=normalized_names,
            ).values_list("normalized_name", flat=True),
        )
        ProviderGenre.objects.bulk_create(
            [
                ProviderGenre(
                    name=display_name,
                    normalized_name=normalized_name,
                )
                for display_name, normalized_name in genres
                if normalized_name not in existing_names
            ],
            ignore_conflicts=True,
        )
        provider_genre_ids = list(
            ProviderGenre.objects.filter(
                normalized_name__in=normalized_names,
            ).values_list("id", flat=True)
        )

        relation = Item._meta.get_field("provider_genres")
        through_model = relation.remote_field.through
        item_field = relation.m2m_field_name()
        genre_field = relation.m2m_reverse_field_name()
        target_item_ids = [item.id for item in target_items]
        through_model.objects.filter(
            **{f"{item_field}_id__in": target_item_ids},
        ).delete()
        through_model.objects.bulk_create(
            [
                through_model(
                    **{
                        f"{item_field}_id": item_id,
                        f"{genre_field}_id": genre_id,
                    },
                )
                for item_id in target_item_ids
                for genre_id in provider_genre_ids
            ],
            ignore_conflicts=True,
        )

    logger.info(
        "Synchronized %s provider genres across %s item(s) for %s/%s/%s",
        len(provider_genre_ids),
        len(target_items),
        source,
        media_type,
        media_id,
    )
    return ProviderGenreSyncResult(
        genre_count=len(provider_genre_ids),
        item_count=len(target_items),
    )
