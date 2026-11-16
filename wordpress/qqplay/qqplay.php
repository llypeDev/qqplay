<?php
/**
 * Plugin Name:       QQplay
 * Description:       Lives with chat and videos with comments from your QQplay portal inside WordPress, with your students already signed in by the site.
 * Version:           1.0.0
 * Requires at least: 5.8
 * Requires PHP:      7.4
 * Author:            QQplay
 * License:           MIT
 * Text Domain:       qqplay
 * Domain Path:       /languages
 *
 * What it does:
 *
 * - [qqplay_live id="..."] and [qqplay_video id="..."] show a live with its
 *   chat or a video with its comments. A portal address pasted on a line of
 *   its own does the same.
 * - Whoever is signed in to WordPress writes there with their own name and
 *   picture: the site signs a short-lived token for them with the
 *   integration's secret, which never leaves the server. The token is asked
 *   for by the page when it opens, not written into it, so a page cache can
 *   never hand one person's identity to another.
 * - The courses someone is enrolled in (LearnDash, Tutor LMS, LifterLMS)
 *   become their classes, and the roles chosen in the settings moderate.
 *
 * @package QQplay
 */

defined( 'ABSPATH' ) || exit;

define( 'QQPLAY_VERSION', '1.0.0' );
define( 'QQPLAY_FILE', __FILE__ );

require_once __DIR__ . '/includes/settings.php';
require_once __DIR__ . '/includes/token.php';
require_once __DIR__ . '/includes/embed.php';

if ( is_admin() ) {
	require_once __DIR__ . '/includes/admin.php';
}

add_action(
	'init',
	static function () {
		load_plugin_textdomain( 'qqplay', false, dirname( plugin_basename( QQPLAY_FILE ) ) . '/languages' );
	}
);

/**
 * The plugin's description, as in the header above. Never called: it is
 * here so that the catalog has it and the list of plugins shows it
 * translated.
 *
 * @return string
 */
function qqplay_description() {
	return __( 'Lives with chat and videos with comments from your QQplay portal inside WordPress, with your students already signed in by the site.', 'qqplay' );
}

register_activation_hook(
	QQPLAY_FILE,
	static function () {
		// The secret is read only when a token is signed: keep it out of the
		// options every page loads.
		add_option( 'qqplay_secret', '', '', false );
	}
);
