<?php
/**
 * Settings › QQplay: the connection with the portal, who moderates, and the
 * lives and videos with the shortcode of each.
 *
 * @package QQplay
 */

defined( 'ABSPATH' ) || exit;

/**
 * The settings and what is accepted in each.
 */
function qqplay_register_settings() {
	register_setting(
		'qqplay',
		'qqplay_portal',
		array(
			'type'              => 'string',
			'sanitize_callback' => 'qqplay_sanitize_portal',
			'default'           => '',
		)
	);
	register_setting(
		'qqplay',
		'qqplay_key',
		array(
			'type'              => 'string',
			'sanitize_callback' => 'qqplay_sanitize_key',
			'default'           => '',
		)
	);
	// No default here: with one, WordPress takes an empty secret for an
	// option that does not exist and adds it again as one every page loads.
	register_setting(
		'qqplay',
		'qqplay_secret',
		array(
			'type'              => 'string',
			'sanitize_callback' => 'qqplay_sanitize_secret',
		)
	);
	register_setting(
		'qqplay',
		'qqplay_teacher_roles',
		array(
			'type'              => 'array',
			'sanitize_callback' => 'qqplay_sanitize_roles',
			'default'           => null,
		)
	);
}
add_action( 'admin_init', 'qqplay_register_settings' );

/**
 * The secret is read only to sign a token: it stays out of the options
 * every page loads, however it came to be saved.
 */
function qqplay_keep_secret_apart() {
	if ( function_exists( 'wp_set_option_autoload' ) ) {
		wp_set_option_autoload( 'qqplay_secret', false );
	}
}
add_action( 'add_option_qqplay_secret', 'qqplay_keep_secret_apart' );
add_action( 'update_option_qqplay_secret', 'qqplay_keep_secret_apart' );

/**
 * The portal's address, down to its origin.
 *
 * @param mixed $value What was typed.
 * @return string
 */
function qqplay_sanitize_portal( $value ) {
	$value = trim( (string) $value );
	if ( '' === $value ) {
		return '';
	}
	// "videos.escola.com.br" is taken as https; another scheme is refused.
	if ( false === strpos( $value, ':' ) || preg_match( '#^[^/:]+:\d+(/|$)#', $value ) ) {
		$value = 'https://' . $value;
	}
	$clean = qqplay_clean_portal( $value );
	if ( '' === $clean ) {
		add_settings_error( 'qqplay', 'qqplay_portal', __( 'The portal\'s address does not look like one: it starts with https://.', 'qqplay' ) );
		return (string) get_option( 'qqplay_portal', '' );
	}
	return $clean;
}

/**
 * The integration's key.
 *
 * @param mixed $value What was typed.
 * @return string
 */
function qqplay_sanitize_key( $value ) {
	return substr( preg_replace( '/[^A-Za-z0-9_-]/', '', (string) $value ), 0, 60 );
}

/**
 * The secret: an empty field keeps the one that is saved.
 *
 * @param mixed $value What was typed.
 * @return string
 */
function qqplay_sanitize_secret( $value ) {
	$value = trim( (string) $value );
	if ( '' === $value ) {
		return (string) get_option( 'qqplay_secret', '' );
	}
	return substr( preg_replace( '/\s+/', '', $value ), 0, 200 );
}

/**
 * The roles that moderate: only ones this site has.
 *
 * @param mixed $value The boxes that were ticked.
 * @return string[]
 */
function qqplay_sanitize_roles( $value ) {
	$chosen = array();
	foreach ( (array) $value as $slug ) {
		$slug = sanitize_key( (string) $slug );
		if ( '' !== $slug && wp_roles()->is_role( $slug ) ) {
			$chosen[] = $slug;
		}
	}
	return array_values( array_unique( $chosen ) );
}

/**
 * The page in the menu, and its link in the list of plugins.
 */
function qqplay_admin_menu() {
	add_options_page( 'QQplay', 'QQplay', 'manage_options', 'qqplay', 'qqplay_settings_page' );
}
add_action( 'admin_menu', 'qqplay_admin_menu' );

add_filter(
	'plugin_action_links_' . plugin_basename( QQPLAY_FILE ),
	static function ( $links ) {
		array_unshift( $links, '<a href="' . esc_url( admin_url( 'options-general.php?page=qqplay' ) ) . '">' . esc_html__( 'Settings', 'qqplay' ) . '</a>' );
		return $links;
	}
);

/**
 * Ask the portal's API something, as this site's server.
 *
 * @param string $path What comes after /api/integrations/v1/.
 * @return array|WP_Error The answer, or why there is none.
 */
function qqplay_api_get( $path ) {
	$response = wp_remote_get(
		qqplay_setting( 'portal' ) . '/api/integrations/v1/' . ltrim( $path, '/' ),
		array(
			'timeout' => 8,
			'headers' => array(
				'Authorization' => 'Bearer ' . qqplay_api_token(),
				'Accept'        => 'application/json',
			),
		)
	);
	if ( is_wp_error( $response ) ) {
		return $response;
	}
	$status = (int) wp_remote_retrieve_response_code( $response );
	$data   = json_decode( (string) wp_remote_retrieve_body( $response ), true );
	if ( $status < 200 || $status >= 300 ) {
		$why = is_array( $data ) && ! empty( $data['detail'] ) ? (string) $data['detail'] : sprintf( 'HTTP %d', $status );
		return new WP_Error( 'qqplay_api', $why, array( 'status' => $status ) );
	}
	if ( ! is_array( $data ) ) {
		return new WP_Error( 'qqplay_api', __( 'The answer could not be read: is this the address of a QQplay portal?', 'qqplay' ) );
	}
	return $data;
}

/**
 * Whether the integration's list of sites lets this one in. An empty list
 * lets every site in; "https://*.escola.com.br" takes every subdomain.
 *
 * @param string[] $sites  The integration's sites.
 * @param string   $origin This site.
 * @return bool
 */
function qqplay_site_is_listed( array $sites, $origin ) {
	if ( ! $sites ) {
		return true;
	}
	$scheme = (string) wp_parse_url( $origin, PHP_URL_SCHEME );
	$host   = substr( $origin, strlen( $scheme ) + 3 );
	foreach ( $sites as $site ) {
		$site = strtolower( (string) $site );
		if ( $site === $origin ) {
			return true;
		}
		$site_scheme = (string) wp_parse_url( $site, PHP_URL_SCHEME );
		$site_host   = substr( $site, strlen( $site_scheme ) + 3 );
		if ( 0 === strpos( $site_host, '*.' ) && $site_scheme === $scheme ) {
			$tail = substr( $site_host, 1 );
			if ( strlen( $host ) > strlen( $tail ) && substr( $host, -strlen( $tail ) ) === $tail ) {
				return true;
			}
		}
	}
	return false;
}

/**
 * A field that copies itself when clicked.
 *
 * @param string $text What it holds.
 */
function qqplay_copy_field( $text ) {
	echo '<input type="text" class="regular-text code" readonly value="' . esc_attr( $text ) . '" onclick="this.select()">';
}

/**
 * The connection's state: who the portal says we are, and what is missing.
 */
function qqplay_connection_box() {
	if ( ! qqplay_can_show() ) {
		echo '<p>' . esc_html__( 'Fill in the portal\'s address and the integration\'s key and secret, and save.', 'qqplay' ) . '</p>';
		return null;
	}
	if ( ! qqplay_can_sign() ) {
		echo '<div class="notice notice-warning inline"><p>' . esc_html__( 'Without the secret, lives and videos show but nobody is signed in: the chat and the comments can only be read.', 'qqplay' ) . '</p></div>';
		return null;
	}
	$about = qqplay_api_get( '' );
	if ( is_wp_error( $about ) ) {
		echo '<div class="notice notice-error inline"><p><strong>' . esc_html__( 'Not connected.', 'qqplay' ) . '</strong> ' . esc_html( $about->get_error_message() ) . '</p></div>';
		return null;
	}
	$integration = isset( $about['integration'] ) && is_array( $about['integration'] ) ? $about['integration'] : array();
	$name        = isset( $integration['name'] ) ? (string) $integration['name'] : '';
	$portal      = isset( $about['portal'] ) ? (string) $about['portal'] : 'QQplay';
	echo '<div class="notice notice-success inline"><p>';
	/* translators: 1: the integration's name, 2: the portal's name */
	echo esc_html( sprintf( __( 'Connected to the integration "%1$s" on %2$s.', 'qqplay' ), $name, $portal ) );
	echo '</p></div>';

	$sites  = isset( $integration['sites'] ) && is_array( $integration['sites'] ) ? $integration['sites'] : array();
	$origin = qqplay_site_origin();
	if ( ! qqplay_site_is_listed( $sites, $origin ) ) {
		echo '<div class="notice notice-warning inline"><p>';
		echo esc_html__( 'This site is not in the integration\'s list of sites, so its pages cannot sign people in yet. On the portal, in the integration, add this address to the sites:', 'qqplay' );
		echo '</p><p>';
		qqplay_copy_field( $origin );
		echo '</p></div>';
	}
	return $about;
}

/**
 * The lives and videos of the integration's owner, with their shortcodes.
 */
function qqplay_content_tables() {
	$lives  = qqplay_api_get( 'lives/' );
	$videos = qqplay_api_get( 'videos/' );

	echo '<h2>' . esc_html__( 'Lives', 'qqplay' ) . '</h2>';
	if ( is_wp_error( $lives ) || empty( $lives['lives'] ) ) {
		echo '<p>' . esc_html__( 'No lives yet: schedule one in the portal\'s studio.', 'qqplay' ) . '</p>';
	} else {
		$states = array(
			'scheduled'   => __( 'Scheduled', 'qqplay' ),
			'live'        => __( 'On air', 'qqplay' ),
			'interrupted' => __( 'Interrupted', 'qqplay' ),
			'ended'       => __( 'Ended', 'qqplay' ),
		);
		echo '<table class="widefat striped" style="max-width:900px"><thead><tr><th>' . esc_html__( 'Title', 'qqplay' ) . '</th><th>' . esc_html__( 'State', 'qqplay' ) . '</th><th>' . esc_html__( 'Classes', 'qqplay' ) . '</th><th>' . esc_html__( 'Shortcode', 'qqplay' ) . '</th></tr></thead><tbody>';
		foreach ( array_slice( $lives['lives'], 0, 50 ) as $live ) {
			$state   = isset( $live['status'], $states[ $live['status'] ] ) ? $states[ $live['status'] ] : '';
			$classes = isset( $live['classes'] ) && is_array( $live['classes'] ) ? implode( ', ', array_map( 'strval', $live['classes'] ) ) : '';
			echo '<tr><td>' . esc_html( (string) $live['title'] ) . '</td><td>' . esc_html( $state ) . '</td><td>' . esc_html( $classes ) . '</td><td>';
			qqplay_copy_field( '[qqplay_live id="' . $live['id'] . '"]' );
			echo '</td></tr>';
		}
		echo '</tbody></table>';
	}

	echo '<h2>' . esc_html__( 'Videos', 'qqplay' ) . '</h2>';
	if ( is_wp_error( $videos ) || empty( $videos['videos'] ) ) {
		echo '<p>' . esc_html__( 'No videos yet: upload one in the portal.', 'qqplay' ) . '</p>';
	} else {
		echo '<table class="widefat striped" style="max-width:900px"><thead><tr><th>' . esc_html__( 'Title', 'qqplay' ) . '</th><th>' . esc_html__( 'Shortcode', 'qqplay' ) . '</th></tr></thead><tbody>';
		foreach ( array_slice( $videos['videos'], 0, 100 ) as $video ) {
			echo '<tr><td>' . esc_html( (string) $video['title'] ) . '</td><td>';
			qqplay_copy_field( '[qqplay_video id="' . $video['id'] . '"]' );
			echo '</td></tr>';
		}
		echo '</tbody></table>';
	}
	echo '<p class="description">' . esc_html__( 'Paste the shortcode in a page, a lesson or a post. Add layout="player" to show only the player.', 'qqplay' ) . '</p>';
}

/**
 * The classes this site sends: the courses of the course plugins in use.
 */
function qqplay_classes_table() {
	$types = qqplay_class_post_types();
	echo '<h2>' . esc_html__( 'Classes', 'qqplay' ) . '</h2>';
	if ( ! $types ) {
		echo '<p>' . esc_html__( 'With LearnDash, Tutor LMS or LifterLMS, the courses a student is enrolled in become their classes. Without them, every signed-in person is in no class, and private lives are open to all of them. The qqplay_user_classes filter says otherwise.', 'qqplay' ) . '</p>';
		return;
	}
	$courses = get_posts(
		array(
			'post_type'   => $types,
			'post_status' => 'publish',
			'numberposts' => 100,
			'orderby'     => 'title',
			'order'       => 'ASC',
		)
	);
	echo '<p>' . esc_html__( 'A student\'s classes are the courses they are enrolled in. To keep a live for one course, write the class below in the live\'s classes on the portal.', 'qqplay' ) . '</p>';
	if ( ! $courses ) {
		echo '<p>' . esc_html__( 'No published courses yet.', 'qqplay' ) . '</p>';
		return;
	}
	echo '<table class="widefat striped" style="max-width:900px"><thead><tr><th>' . esc_html__( 'Course', 'qqplay' ) . '</th><th>' . esc_html__( 'Class', 'qqplay' ) . '</th></tr></thead><tbody>';
	foreach ( $courses as $course ) {
		echo '<tr><td>' . esc_html( get_the_title( $course ) ) . '</td><td>';
		qqplay_copy_field( qqplay_class_name( $course->ID ) );
		echo '</td></tr>';
	}
	echo '</tbody></table>';
}

/**
 * The page.
 */
function qqplay_settings_page() {
	if ( ! current_user_can( 'manage_options' ) ) {
		return;
	}
	$chosen = qqplay_teacher_roles();
	?>
	<div class="wrap">
		<h1>QQplay</h1>
		<p><?php esc_html_e( 'Lives with chat and videos with comments from your QQplay portal, with the people of this site already signed in.', 'qqplay' ); ?></p>
		<?php settings_errors( 'qqplay' ); ?>

		<form method="post" action="options.php">
			<?php settings_fields( 'qqplay' ); ?>
			<h2><?php esc_html_e( 'Connection', 'qqplay' ); ?></h2>
			<p class="description"><?php esc_html_e( 'The three are on the integration\'s page, in the portal\'s studio (Studio › Integrations).', 'qqplay' ); ?></p>
			<table class="form-table" role="presentation">
				<tr>
					<th scope="row"><label for="qqplay_portal"><?php esc_html_e( 'Portal address', 'qqplay' ); ?></label></th>
					<td>
						<input name="qqplay_portal" id="qqplay_portal" type="url" class="regular-text code" placeholder="https://videos.escola.com.br" value="<?php echo esc_attr( qqplay_setting( 'portal' ) ); ?>" <?php disabled( qqplay_is_fixed( 'portal' ) ); ?>>
						<?php qqplay_fixed_note( 'portal' ); ?>
					</td>
				</tr>
				<tr>
					<th scope="row"><label for="qqplay_key"><?php esc_html_e( 'Key', 'qqplay' ); ?></label></th>
					<td>
						<input name="qqplay_key" id="qqplay_key" type="text" class="regular-text code" placeholder="sch_…" value="<?php echo esc_attr( qqplay_setting( 'key' ) ); ?>" <?php disabled( qqplay_is_fixed( 'key' ) ); ?>>
						<?php qqplay_fixed_note( 'key' ); ?>
					</td>
				</tr>
				<tr>
					<th scope="row"><label for="qqplay_secret"><?php esc_html_e( 'Secret', 'qqplay' ); ?></label></th>
					<td>
						<input name="qqplay_secret" id="qqplay_secret" type="password" class="regular-text code" autocomplete="new-password" value="" placeholder="<?php echo esc_attr( '' !== qqplay_setting( 'secret' ) ? __( 'saved: leave empty to keep it', 'qqplay' ) : '' ); ?>" <?php disabled( qqplay_is_fixed( 'secret' ) ); ?>>
						<?php qqplay_fixed_note( 'secret' ); ?>
						<p class="description"><?php esc_html_e( 'It stays on this server and signs each person\'s entry. To keep it out of the database, define QQPLAY_SECRET in wp-config.php.', 'qqplay' ); ?></p>
					</td>
				</tr>
				<tr>
					<th scope="row"><?php esc_html_e( 'Who moderates', 'qqplay' ); ?></th>
					<td>
						<fieldset>
							<input type="hidden" name="qqplay_teacher_roles[]" value="">
							<?php foreach ( wp_roles()->get_names() as $slug => $label ) : ?>
								<label>
									<input type="checkbox" name="qqplay_teacher_roles[]" value="<?php echo esc_attr( $slug ); ?>" <?php checked( in_array( $slug, $chosen, true ) ); ?>>
									<?php echo esc_html( translate_user_role( $label ) ); ?>
								</label><br>
							<?php endforeach; ?>
						</fieldset>
						<p class="description"><?php esc_html_e( 'People with these roles can hide messages, answer questions and pin a message in the chat, and open the lives of every class.', 'qqplay' ); ?></p>
					</td>
				</tr>
			</table>
			<?php submit_button(); ?>
		</form>

		<h2><?php esc_html_e( 'State', 'qqplay' ); ?></h2>
		<?php
		$about = qqplay_connection_box();
		if ( null !== $about ) {
			qqplay_content_tables();
		}
		qqplay_classes_table();
		?>
	</div>
	<?php
}

/**
 * Says that a field is fixed in wp-config.php.
 *
 * @param string $name portal, key or secret.
 */
function qqplay_fixed_note( $name ) {
	if ( qqplay_is_fixed( $name ) ) {
		echo '<p class="description">' . esc_html__( 'Defined in wp-config.php.', 'qqplay' ) . '</p>';
	}
}
