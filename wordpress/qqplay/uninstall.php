<?php
/**
 * Removing the plugin takes its settings with it.
 *
 * @package QQplay
 */

defined( 'WP_UNINSTALL_PLUGIN' ) || exit;

foreach ( array( 'qqplay_portal', 'qqplay_key', 'qqplay_secret', 'qqplay_teacher_roles' ) as $qqplay_option ) {
	delete_option( $qqplay_option );
}
