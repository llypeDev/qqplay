<?php
/**
 * What the plugin was told: the portal, the integration's key and secret,
 * and who moderates.
 *
 * Each of the three can also be fixed in wp-config.php, which keeps the
 * secret out of the database:
 *
 *     define( 'QQPLAY_PORTAL', 'https://videos.escola.com.br' );
 *     define( 'QQPLAY_KEY', 'sch_...' );
 *     define( 'QQPLAY_SECRET', '...' );
 *
 * @package QQplay
 */

defined( 'ABSPATH' ) || exit;

/**
 * The names of the constants that fix a setting.
 *
 * @return array<string,string>
 */
function qqplay_constants() {
	return array(
		'portal' => 'QQPLAY_PORTAL',
		'key'    => 'QQPLAY_KEY',
		'secret' => 'QQPLAY_SECRET',
	);
}

/**
 * Whether a setting is fixed in wp-config.php.
 *
 * @param string $name portal, key or secret.
 * @return bool
 */
function qqplay_is_fixed( $name ) {
	$constants = qqplay_constants();
	return isset( $constants[ $name ] ) && defined( $constants[ $name ] );
}

/**
 * A portal address as the plugin uses it: scheme and host (and port), with
 * nothing after them. '' when it is not an http(s) address.
 *
 * @param string $value What was typed.
 * @return string
 */
function qqplay_clean_portal( $value ) {
	$parts = wp_parse_url( trim( (string) $value ) );
	if ( empty( $parts['host'] ) || empty( $parts['scheme'] ) || ! in_array( strtolower( $parts['scheme'] ), array( 'http', 'https' ), true ) ) {
		return '';
	}
	$origin = strtolower( $parts['scheme'] ) . '://' . strtolower( $parts['host'] );
	if ( ! empty( $parts['port'] ) ) {
		$origin .= ':' . (int) $parts['port'];
	}
	return $origin;
}

/**
 * One of the settings.
 *
 * @param string $name portal, key or secret.
 * @return string
 */
function qqplay_setting( $name ) {
	$constants = qqplay_constants();
	if ( qqplay_is_fixed( $name ) ) {
		$value = (string) constant( $constants[ $name ] );
	} else {
		$value = (string) get_option( 'qqplay_' . $name, '' );
	}
	$value = trim( $value );
	return 'portal' === $name ? qqplay_clean_portal( $value ) : $value;
}

/**
 * Whether lives and videos can be shown: the portal and the key are known.
 *
 * @return bool
 */
function qqplay_can_show() {
	return '' !== qqplay_setting( 'portal' ) && '' !== qqplay_setting( 'key' );
}

/**
 * Whether people can be signed in: there is a secret to sign with.
 *
 * @return bool
 */
function qqplay_can_sign() {
	return qqplay_can_show() && '' !== qqplay_setting( 'secret' );
}

/**
 * The roles that moderate when nothing was chosen: whoever may edit other
 * people's posts, and the teachers of the course plugins that are active.
 *
 * @return string[]
 */
function qqplay_default_teacher_roles() {
	$roles = array();
	foreach ( wp_roles()->roles as $slug => $role ) {
		if ( ! empty( $role['capabilities']['edit_others_posts'] ) ) {
			$roles[] = $slug;
		}
	}
	// Tutor LMS, LearnDash, LifterLMS.
	foreach ( array( 'tutor_instructor', 'group_leader', 'instructor', 'instructors_assistant', 'lms_manager' ) as $slug ) {
		if ( wp_roles()->is_role( $slug ) ) {
			$roles[] = $slug;
		}
	}
	return array_values( array_unique( $roles ) );
}

/**
 * The roles whose people moderate the chat and the comments.
 *
 * @return string[]
 */
function qqplay_teacher_roles() {
	$chosen = get_option( 'qqplay_teacher_roles', null );
	if ( ! is_array( $chosen ) ) {
		return qqplay_default_teacher_roles();
	}
	return array_values( array_filter( array_map( 'strval', $chosen ) ) );
}

/**
 * This site's address as the integration must list it.
 *
 * @return string
 */
function qqplay_site_origin() {
	return qqplay_clean_portal( home_url() );
}
