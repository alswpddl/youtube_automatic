"""쇼츠 트렌드 수집: 최근 업로드된 짧은 영상을 조회순 50개 수집하고 주제 3개를 선정한다.

표준 라이브러리만 사용한다 (pip 설치 불필요).
필요한 환경변수: YOUTUBE_API_KEY
"""
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

API = "https://www.googleapis.com/youtube/v3"
KST = timezone(timedelta(hours=9))
DATA_DIR = Path("data/trends")
LOOKBACK_HOURS = 48
MAX_RESULTS = 50
PICK_COUNT = 3
EXCLUDE_DAYS = 3  # 최근 N일 안에 선정한 주제는 제외

STOPWORDS = {
    "shorts", "short", "youtube", "영상", "유튜브", "쇼츠", "오늘", "진짜", "ㅋㅋ", "ㅋㅋㅋ",
    "the", "and", "for", "you", "with", "this", "that", "from", "are", "was", "how",
    "하는", "있는", "이거", "그냥", "너무", "정말", "그리고", "하지만", "근데", "이번",
}


def api_get(endpoint, params):
    params = dict(params)
    params["key"] = os.environ["YOUTUBE_API_KEY"]
    url = f"{API}/{endpoint}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:500]
        raise SystemExit(f"API 오류 {e.code} ({endpoint}): {body}")


def search_video_ids(now):
    published_after = (now - timedelta(hours=LOOKBACK_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = api_get("search", {
        "part": "id",
        "type": "video",
        "videoDuration": "short",
        "order": "viewCount",
        "publishedAfter": published_after,
        "regionCode": "KR",
        "relevanceLanguage": "ko",
        "maxResults": MAX_RESULTS,
    })
    return [item["id"]["videoId"] for item in data.get("items", [])]


def parse_duration(iso):
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return 0
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def is_shorts_url(video_id):
    """youtube.com/shorts/ID 가 리다이렉트 없이 200이면 쇼츠. 실패하면 None."""
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(
        f"https://www.youtube.com/shorts/{video_id}", method="HEAD",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    try:
        with opener.open(req, timeout=10) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as e:
        return False if 300 <= e.code < 400 else None
    except Exception:
        return None


def fetch_videos(ids, now):
    data = api_get("videos", {
        "part": "snippet,statistics,contentDetails",
        "id": ",".join(ids),
        "maxResults": MAX_RESULTS,
    })
    videos = []
    for item in data.get("items", []):
        sn, st = item["snippet"], item.get("statistics", {})
        published = datetime.fromisoformat(sn["publishedAt"].replace("Z", "+00:00"))
        hours = max((now - published).total_seconds() / 3600, 1.0)
        views = int(st.get("viewCount", 0))
        duration = parse_duration(item["contentDetails"].get("duration"))
        text = f"{sn.get('title', '')} {sn.get('description', '')}".lower()
        tagged = "#shorts" in text or "#쇼츠" in text
        url_check = is_shorts_url(item["id"])
        is_shorts = (duration <= 180) and (url_check if url_check is not None else tagged or duration <= 60)
        videos.append({
            "id": item["id"],
            "title": sn.get("title", ""),
            "channel": sn.get("channelTitle", ""),
            "published_at": sn["publishedAt"],
            "duration_sec": duration,
            "views": views,
            "likes": int(st.get("likeCount", 0)),
            "comments": int(st.get("commentCount", 0)),
            "views_per_hour": round(views / hours, 1),
            "tags": sn.get("tags", []),
            "category_id": sn.get("categoryId"),
            "is_shorts": bool(is_shorts),
        })
    videos.sort(key=lambda v: v["views"], reverse=True)
    return videos


def extract_keywords(video):
    words = set()
    text = video["title"] + " " + " ".join(video["tags"])
    for tok in re.findall(r"[0-9A-Za-z가-힣]{2,}", text):
        tok = tok.lower()
        if tok in STOPWORDS or tok.isdigit():
            continue
        words.add(tok)
    return words


def score_keywords(videos):
    scores = defaultdict(lambda: {"count": 0, "score": 0.0, "videos": []})
    for v in videos:
        for kw in extract_keywords(v):
            s = scores[kw]
            s["count"] += 1
            s["score"] += v["views_per_hour"]
            s["videos"].append(v["id"])
    # 2개 이상 영상에 등장한 키워드만 주제 후보
    ranked = [(k, v) for k, v in scores.items() if v["count"] >= 2]
    ranked.sort(key=lambda kv: kv[1]["score"], reverse=True)
    return ranked


def recent_picks(today):
    picked = set()
    for i in range(1, EXCLUDE_DAYS + 1):
        path = DATA_DIR / f"{(today - timedelta(days=i)).isoformat()}.json"
        if path.exists():
            try:
                for p in json.loads(path.read_text(encoding="utf-8")).get("picks", []):
                    picked.add(p["keyword"])
            except (ValueError, KeyError):
                pass
    return picked


def main():
    if not os.environ.get("YOUTUBE_API_KEY"):
        raise SystemExit("YOUTUBE_API_KEY 환경변수가 없습니다.")
    now = datetime.now(timezone.utc)
    today = now.astimezone(KST).date()

    ids = search_video_ids(now)
    if not ids:
        raise SystemExit("검색 결과가 없습니다.")
    videos = fetch_videos(ids, now)
    shorts = [v for v in videos if v["is_shorts"]]
    pool = shorts if len(shorts) >= 10 else videos  # 쇼츠 판별이 너무 적으면 전체 사용

    ranked = score_keywords(pool)
    excluded = recent_picks(today)
    by_id = {v["id"]: v for v in pool}
    picks = []
    for kw, info in ranked:
        if kw in excluded:
            continue
        picks.append({
            "keyword": kw,
            "score": round(info["score"], 1),
            "video_count": info["count"],
            "evidence": [
                {"id": vid, "title": by_id[vid]["title"], "views_per_hour": by_id[vid]["views_per_hour"]}
                for vid in info["videos"][:5]
            ],
        })
        if len(picks) == PICK_COUNT:
            break

    out = {
        "date": today.isoformat(),
        "collected": len(videos),
        "shorts_count": len(shorts),
        "picks": picks,
        "keywords": [
            {"keyword": k, "count": v["count"], "score": round(v["score"], 1)} for k, v in ranked[:30]
        ],
        "videos": pool,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"{today.isoformat()}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"수집 {len(videos)}개 / 쇼츠 {len(shorts)}개 → {path}")
    for p in picks:
        print(f"  주제: {p['keyword']} (점수 {p['score']}, 영상 {p['video_count']}개)")
    if len(picks) < PICK_COUNT:
        print(f"경고: 주제가 {len(picks)}개만 선정됨", file=sys.stderr)


if __name__ == "__main__":
    main()
