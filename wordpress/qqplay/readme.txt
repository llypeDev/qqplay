=== QQplay ===
Contributors: qqplay
Tags: video, live, lms, courses, embed
Requires at least: 5.8
Tested up to: 7.1
Requires PHP: 7.4
Stable tag: 1.0.0
License: MIT

Lives with chat and videos with comments from your QQplay portal, with the people of your site already signed in.

== Description ==

QQplay is a video portal you run on your own server. This plugin puts its lives and videos in your WordPress pages and lessons:

* `[qqplay_live id="..."]` shows a live with its chat, `[qqplay_video id="..."]` a video with its comments. A portal address pasted on a line of its own does the same.
* Whoever is signed in to WordPress writes with their own name and picture, without another account or password.
* The courses a student is enrolled in (LearnDash, Tutor LMS, LifterLMS) are their classes: a private live can be kept for one course.
* The roles you choose moderate the chat: hide messages, answer questions, pin a message.

The integration's secret stays on your server. The page asks for the visitor's entry when it opens instead of carrying it, so page caches are safe.

== Installation ==

1. In the portal's studio, open Studio › Integrations, create an integration with your site's address and download this plugin from its page.
2. In WordPress, Plugins › Add New › Upload Plugin, choose the file and activate it.
3. In Settings › QQplay, fill in the portal's address and the integration's key and secret.
4. Copy the shortcode of a live or a video from that same page into a page or a lesson.

To keep the secret out of the database, define it in wp-config.php:

`define( 'QQPLAY_SECRET', '...' );`

(`QQPLAY_PORTAL` and `QQPLAY_KEY` work the same way.)

== Frequently Asked Questions ==

= Which attributes do the shortcodes take? =

`layout` (both, player, chat or comments), `theme` (dark or light) and `height` (of the chat alone, in pixels). `[qqplay url="..."]` takes the address of a live or a video instead of its id.

= How do I decide someone's classes or who moderates myself? =

With the filters `qqplay_user_classes`, `qqplay_user_is_teacher` and `qqplay_token_claims`.

== Changelog ==

= 1.0.0 =
* First version.
